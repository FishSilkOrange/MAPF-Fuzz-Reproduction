"""Small, independent first stage of the MAPF-Fuzz course reproduction.

Generate MAPF instances, run one solver on symmetry-related instances, and
write replayable inputs and JSONL results. Python standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import subprocess
import tempfile
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIBT = ROOT / "solvers/pibt2"
LACAM = ROOT / "solvers/lacam3"
Cell = tuple[int, int]  # row, column
Pair = tuple[Cell, Cell]
COORD_RE = re.compile(r"\((\d+),(\d+)\)")  # solver writes (column,row)


@dataclass(frozen=True)
class Task:
    grid: tuple[str, ...]
    agents: tuple[Pair, ...]

    @property
    def height(self) -> int:
        return len(self.grid)

    @property
    def width(self) -> int:
        return len(self.grid[0])


def read_map(path: Path, size: int | None) -> tuple[str, ...]:
    lines = path.read_text().splitlines()
    try:
        marker = next(i for i, line in enumerate(lines) if line.strip().lower() == "map")
    except StopIteration as exc:
        raise ValueError(f"Missing 'map' header: {path}") from exc
    raw = [line.strip() for line in lines[marker + 1 :] if line.strip()]
    width = int(next(line.split()[1] for line in lines[:marker] if line.lower().startswith("width ")))
    height = int(next(line.split()[1] for line in lines[:marker] if line.lower().startswith("height ")))
    if len(raw) != height or any(len(line) != width for line in raw):
        raise ValueError(f"Map dimensions do not match header: {path}")
    # MovingAI traversable terrains include '.' and 'G'; all else is blocked.
    grid = tuple("".join("." if c in ".G" else "@" for c in row) for row in raw)
    if size is not None:
        if size < 2:
            raise ValueError("--size must be at least 2")
        # Nearest-neighbor resize of the whole map, matching the artifact's approach.
        grid = tuple("".join(grid[min(height - 1, r * height // size)]
                             [min(width - 1, c * width // size)] for c in range(size))
                     for r in range(size))
    return grid


def neighbors(grid: tuple[str, ...], cell: Cell):
    row, col = cell
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        r, c = row + dr, col + dc
        if 0 <= r < len(grid) and 0 <= c < len(grid[0]) and grid[r][c] == ".":
            yield r, c


def neighbors_any(grid: tuple[str, ...], cell: Cell):
    row, col = cell
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        r, c = row + dr, col + dc
        if 0 <= r < len(grid) and 0 <= c < len(grid[0]):
            yield r, c


def shortest_path_length(grid: tuple[str, ...], start: Cell, goal: Cell) -> int:
    distances = {start: 0}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        if cell == goal:
            return distances[cell]
        for other in neighbors(grid, cell):
            if other not in distances:
                distances[other] = distances[cell] + 1
                queue.append(other)
    return 0


def dbscan_summary(points: list[Cell], eps: int = 25,
                   min_samples: int = 3) -> tuple[int, float, float]:
    """Exact DBSCAN with Manhattan distance, matching artifact feature settings."""
    labels = [-2] * len(points)
    cluster_id = 0
    for index in range(len(points)):
        if labels[index] != -2:
            continue
        nearby = [j for j, point in enumerate(points)
                  if abs(point[0] - points[index][0]) + abs(point[1] - points[index][1]) <= eps]
        if len(nearby) < min_samples:
            labels[index] = -1
            continue
        labels[index] = cluster_id
        frontier = deque(nearby)
        queued = set(nearby)
        while frontier:
            current = frontier.popleft()
            if labels[current] == -1:
                labels[current] = cluster_id
            if labels[current] != -2:
                continue
            labels[current] = cluster_id
            neighbors_ = [j for j, point in enumerate(points)
                          if abs(point[0] - points[current][0]) + abs(point[1] - points[current][1]) <= eps]
            if len(neighbors_) >= min_samples:
                for neighbor in neighbors_:
                    if neighbor not in queued:
                        queued.add(neighbor)
                        frontier.append(neighbor)
        cluster_id += 1
    sizes = [labels.count(label) for label in range(cluster_id)]
    noise_ratio = labels.count(-1) / len(points) if points else 0.0
    average = sum(sizes) / len(sizes) if sizes else 0.0
    return cluster_id, noise_ratio, average


def seed_features(task: Task, result: dict) -> tuple[float, ...]:
    """Artifact-shaped 16D vector: map (3), agent configuration (8), runtime (5)."""
    free = [(r, c) for r, row in enumerate(task.grid)
            for c, value in enumerate(row) if value == "."]
    distances = {(r, c): 0 for r, row in enumerate(task.grid)
                 for c, value in enumerate(row) if value != "."}
    frontier = deque(distances)
    while frontier:
        cell = frontier.popleft()
        for other in neighbors_any(task.grid, cell):
            if other not in distances:
                distances[other] = distances[cell] + 1
                frontier.append(other)
    avg_gap = sum(distances.get(cell, 0) for cell in free) / max(1, len(free))
    dead_ends = sum(sum(1 for _ in neighbors(task.grid, cell)) == 1 for cell in free)
    starts = [start for start, _ in task.agents]
    goals = [goal for _, goal in task.agents]
    shortest = [shortest_path_length(task.grid, start, goal) for start, goal in task.agents]
    waits = [int(value) for value in result.get("agent_waits", [])]
    if len(waits) != len(task.agents):
        waits = [0] * len(task.agents)
    completion = result.get("agent_completion", [])
    if len(completion) != len(task.agents):
        completion = [distance + wait for distance, wait in zip(shortest, waits)]
    travel = [max(0, total - wait) for total, wait in zip(completion, waits)]
    detours = [total / distance for total, distance in zip(travel, shortest) if distance > 0]
    mean_short = sum(shortest) / max(1, len(shortest))
    mean_wait = sum(waits) / max(1, len(waits))
    mean_detour = sum(detours) / max(1, len(detours))
    features = (
        len(free) / max(1, task.height * task.width), avg_gap, float(dead_ends),
        mean_short,
        (sum((value - mean_short) ** 2 for value in shortest) / max(1, len(shortest))) ** 0.5,
        *dbscan_summary(starts), *dbscan_summary(goals),
        sum(travel) / max(1, sum(shortest)),
        mean_detour,
        (sum((value - mean_detour) ** 2 for value in detours) / max(1, len(detours))) ** 0.5,
        mean_wait,
        (sum((value - mean_wait) ** 2 for value in waits) / max(1, len(waits))) ** 0.5,
    )
    if len(features) != 16:
        raise AssertionError(f"Expected 16 seed features, got {len(features)}")
    return tuple(float(value) for value in features)


def critical_score(task: Task, result: dict) -> float:
    """Paper's four criticality ratios with the reported 2x-mean threshold."""
    frames = result.get("_frames", [])
    completion = result.get("agent_completion", [])
    waits = result.get("agent_waits", [])
    if not frames or len(completion) != len(task.agents) or len(waits) != len(task.agents):
        return 0.0
    shortest = [shortest_path_length(task.grid, start, goal)
                for start, goal in task.agents]
    travel = [max(0, total - wait) for total, wait in zip(completion, waits)]
    raw = sum(value > 2 * (sum(waits) / len(waits)) for value in waits) / len(waits) \
        if sum(waits) else 0.0
    rad = sum(distance > 0 and actual > 2 * distance
              for actual, distance in zip(travel, shortest)) / len(shortest)
    use = Counter()
    cell_wait = Counter()
    for i, finish in enumerate(completion):
        for t in range(1, finish):
            previous, current = frames[t - 1][i], frames[t][i]
            # Count only pre-goal states. Full-coordinate inequality means
            # row != goal_row OR col != goal_col, matching the paper's intent.
            if current != task.agents[i][1]:
                use[current] += 1
                if current == previous:
                    cell_wait[current] += 1
    free_count = sum(row.count(".") for row in task.grid)
    total_cells = task.height * task.width
    mean_use = sum(use.values()) / max(1, total_cells)
    mean_wait = sum(cell_wait.values()) / max(1, total_cells)
    rmu = sum(use[(row, col)] > 2 * mean_use for row in range(task.height)
              for col in range(task.width)) / max(1, free_count) if mean_use else 0.0
    rmw = sum(cell_wait[(row, col)] > 2 * mean_wait for row in range(task.height)
              for col in range(task.width)) / max(1, free_count) if mean_wait else 0.0
    # alpha1=alpha2=2*alpha3=2*alpha4 and sum(alpha)=1.
    return (raw + rad) / 3 + (rmu + rmw) / 6


def normalized_neighbor_distances(features: tuple[float, ...],
                                  vectors: list[tuple[float, ...]]) -> tuple[float, float | None] | None:
    if not vectors:
        return None
    dimensions = len(features)
    means = [sum(vector[i] for vector in vectors) / len(vectors)
             for i in range(dimensions)]
    scales = [(sum((vector[i] - means[i]) ** 2 for vector in vectors) /
               len(vectors)) ** 0.5 or 1.0 for i in range(dimensions)]
    def normalize(vector):
        return tuple((value - means[i]) / scales[i] if scales[i] else 0.0
                     for i, value in enumerate(vector))
    current = normalize(features)
    distances = sorted(sum((a - b) ** 2 for a, b in zip(current, normalize(vector))) ** 0.5
                       for vector in vectors)
    return distances[0], distances[1] if len(distances) > 1 else None


def normalized_nearest_distance(features: tuple[float, ...],
                                vectors: list[tuple[float, ...]]) -> float | None:
    result = normalized_neighbor_distances(features, vectors)
    return result[0] if result else None


def diversity_weight(distance: float | None, history: list[float]) -> float:
    """Artifact novelty buckets; exact brute-force NN replaces its ANN index."""
    if distance is None:
        return 1.0
    if len(history) < 20:
        return 0.5 if distance < 0.5 else 1.5 if distance > 2.0 else 1.0
    ordered = sorted(history)
    q1 = ordered[(len(ordered) - 1) // 4]
    q3 = ordered[(3 * (len(ordered) - 1)) // 4]
    return 0.5 if distance < q1 else 1.5 if distance > q3 else 1.0


def select_seed(pool: list[dict], rng: random.Random) -> dict:
    del rng  # Selection is deterministic: the author artifact pops the heap maximum.
    selected = max(pool, key=lambda item: item["energy"])
    selected["energy"] *= 0.9  # FITNESS_DECAY_FACTOR: reduce repeated selection.
    return selected


def length_multiplier(soc: int, agent_count: int, map_size: int = 128) -> float:
    average_length = soc / max(1, agent_count)
    exponent = max(-700.0, min(700.0, -0.03 * (average_length - map_size * 0.8)))
    return 2.0 / (1.0 + math.exp(exponent))


def components(grid: tuple[str, ...]) -> list[list[Cell]]:
    seen: set[Cell] = set()
    groups: list[list[Cell]] = []
    for r, line in enumerate(grid):
        for c, value in enumerate(line):
            if value != "." or (r, c) in seen:
                continue
            group: list[Cell] = []
            queue = deque([(r, c)])
            seen.add((r, c))
            while queue:
                cell = queue.popleft()
                group.append(cell)
                for other in neighbors(grid, cell):
                    if other not in seen:
                        seen.add(other)
                        queue.append(other)
            groups.append(group)
    return groups


def generate_task(grid: tuple[str, ...], count: int, rng: random.Random) -> Task:
    # One connected region and distinct endpoints make small examples reliable.
    eligible = [group for group in components(grid) if len(group) >= 2 * count]
    if not eligible:
        raise ValueError(f"No connected free-space region has {2 * count} cells")
    cells = max(eligible, key=len)
    endpoints = rng.sample(cells, 2 * count)
    pairs = tuple((endpoints[i], endpoints[count + i]) for i in range(count))
    return Task(grid, pairs)


def valid_task(task: Task) -> bool:
    """Cheap paper-style filter: valid distinct endpoints and connected pairs."""
    endpoints = [cell for pair in task.agents for cell in pair]
    if len(set(endpoints)) != len(endpoints):
        return False
    if any(not (0 <= r < task.height and 0 <= c < task.width)
           or task.grid[r][c] != "." for r, c in endpoints):
        return False
    labels = {cell: number for number, group in enumerate(components(task.grid))
              for cell in group}
    return all(labels[start] == labels[goal] for start, goal in task.agents)


def mutate_agents(parent: Task, feedback: dict, rng: random.Random,
                  guidance_rate: float) -> tuple[Task, str] | None:
    """Representative agent mutations: guided local shift or paper's unguided moves."""
    free = [(r, c) for r, line in enumerate(parent.grid)
            for c, value in enumerate(line) if value == "."]
    waits = feedback.get("agent_waits", [])
    for _ in range(50):
        agents = list(parent.agents)
        if rng.random() < guidance_rate:
            index = max(range(len(agents)), key=lambda i: waits[i]) if len(waits) == len(agents) \
                else rng.randrange(len(agents))
            side = rng.randrange(2)
            anchor = agents[index][side]
            candidates = [(r, c) for r, c in free
                          if 0 < abs(r-anchor[0]) + abs(c-anchor[1]) <= 2]
            if not candidates:
                continue
            pair = list(agents[index])
            pair[side] = rng.choice(candidates)
            agents[index] = tuple(pair)
            operation = "agent_guided_local_shift"
        else:
            operation = rng.choice(("agent_swap_subset", "agent_permute_goals",
                                    "agent_relocate_endpoint", "agent_regenerate"))
            if operation == "agent_swap_subset":
                selected = rng.sample(range(len(agents)), rng.randint(1, len(agents)))
                for index in selected:
                    start, goal = agents[index]
                    agents[index] = (goal, start)
            elif operation == "agent_permute_goals":
                if len(agents) < 2:
                    continue
                goals = [goal for _, goal in agents]
                rng.shuffle(goals)
                agents = [(agents[i][0], goals[i]) for i in range(len(agents))]
            elif operation == "agent_relocate_endpoint":
                index = rng.randrange(len(agents))
                side = rng.randrange(2)
                pair = list(agents[index])
                pair[side] = rng.choice(free)
                agents[index] = tuple(pair)
            else:
                try:
                    candidate = generate_task(parent.grid, len(agents), rng)
                except ValueError:
                    continue
                if candidate != parent:
                    return candidate, operation
                continue
        candidate = Task(parent.grid, tuple(agents))
        if candidate != parent and valid_task(candidate):
            return candidate, operation
    return None


def mutate_map(parent: Task, feedback: dict, rng: random.Random,
               patch_size: int) -> tuple[Task, str] | None:
    """Noise, cellular-automata, or DLA patches near heavily used cells."""
    if patch_size > min(parent.height, parent.width):
        raise ValueError("--patch-size exceeds the resized map dimensions")
    hot = [tuple(cell) for cell in feedback.get("hot_cells", [])]
    if not hot:
        hot = [(rng.randrange(parent.height), rng.randrange(parent.width))]
    windows: list[tuple[int, int]] = []
    for row, col in hot:
        top = max(0, min(parent.height - patch_size, row - patch_size // 2))
        left = max(0, min(parent.width - patch_size, col - patch_size // 2))
        if all(abs(top - r) >= patch_size or abs(left - c) >= patch_size
               for r, c in windows):
            windows.append((top, left))
        if len(windows) == 3:
            break
    current = parent
    selected_kinds: list[str] = []
    operations = ("replace", "union", "intersection", "new_minus_old",
                  "old_minus_new", "not_old_union_new", "old_union_not_new")
    weights = (4, 1, 0.3, 0.3, 0.3, 1, 1)
    for top, left in windows:
        for _ in range(50):
            patch_kind = rng.choices(("uniform", "ca", "dla"), weights=(2, 2, 1), k=1)[0]
            old_patch = tuple(row[left:left + patch_size]
                              for row in current.grid[top:top + patch_size])
            new_patch = generate_map_patch(old_patch, patch_kind, rng)
            operation = rng.choices(operations, weights=weights, k=1)[0]
            grid = [list(row) for row in current.grid]
            for dr in range(patch_size):
                for dc in range(patch_size):
                    old = grid[top + dr][left + dc] == "."
                    new = new_patch[dr][dc] == "."
                    value = {
                        "replace": new,
                        "union": old or new,
                        "intersection": old and new,
                        "new_minus_old": new and not old,
                        "old_minus_new": old and not new,
                        "not_old_union_new": not old or new,
                        "old_union_not_new": old or not new,
                    }[operation]
                    grid[top + dr][left + dc] = "." if value else "@"
            candidate = Task(tuple("".join(row) for row in grid), current.agents)
            if candidate != current and valid_task(candidate):
                current = candidate
                selected_kinds.append(patch_kind)
                break
    if not selected_kinds:
        return None
    kind_label = "_".join(selected_kinds)
    return current, f"map_{kind_label}_patch_x{len(selected_kinds)}"


def generate_map_patch(border: tuple[str, ...], kind: str,
                       rng: random.Random) -> tuple[str, ...]:
    """Generate one binary patch; preserve its border from the source map."""
    height, width = len(border), len(border[0])
    if kind == "uniform":
        density = rng.uniform(0.1, 0.3)
        cells = [["@" if rng.random() < density else "." for _ in range(width)]
                 for _ in range(height)]
    elif kind == "ca":
        presets = (
            ((3,), (2, 3)),       # Conway
            ((3, 6), (2, 3)),     # HighLife
            ((3,), (1, 2, 3, 4, 5)),  # Maze
            ((4, 5, 6, 7, 8), (2, 3, 4, 5)),  # Walled Cities
        )
        births, survives = rng.choice(presets)
        cells = [[rng.random() < 0.3 for _ in range(width)] for _ in range(height)]
        cells = [["@" if value else "." for value in row] for row in cells]
        for _ in range(rng.choice((2, 4))):
            updated = [row[:] for row in cells]
            for r in range(1, height - 1):
                for c in range(1, width - 1):
                    count = sum(cells[nr][nc] == "@"
                                for nr in range(max(0, r - 1), min(height, r + 2))
                                for nc in range(max(0, c - 1), min(width, c + 2))
                                if (nr, nc) != (r, c))
                    if cells[r][c] == "@":
                        updated[r][c] = "@" if count in survives else "."
                    else:
                        updated[r][c] = "@" if count in births else "."
            cells = updated
    elif kind == "dla":
        cells = [["." for _ in range(width)] for _ in range(height)]
        for r in range(height):
            cells[r][0], cells[r][-1] = border[r][0], border[r][-1]
        for c in range(width):
            cells[0][c], cells[-1][c] = border[0][c], border[-1][c]
        obstacle_count = sum(row.count("@") for row in cells)
        if height > 2 and width > 2:
            seed = (rng.randrange(1, height - 1), rng.randrange(1, width - 1))
            cells[seed[0]][seed[1]] = "@"
            obstacle_count += 1
            target = 0.3 * height * width
            walker_limit = int(target * 5)
            for _ in range(walker_limit):
                if obstacle_count >= target:
                    break
                r, c = rng.randrange(1, height - 1), rng.randrange(1, width - 1)
                if cells[r][c] == "@":
                    continue
                # Artifact-style random interior walk; cap steps to avoid stalls.
                for _ in range(height * width * 20):
                    dr, dc = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
                    nr, nc = r + dr, c + dc
                    if not (0 < nr < height - 1 and 0 < nc < width - 1):
                        break
                    r, c = nr, nc
                    if any(cells[ar][ac] == "@"
                           for ar, ac in ((r-1,c),(r+1,c),(r,c-1),(r,c+1))):
                        cells[r][c] = "@"
                        obstacle_count += 1
                        break
    else:
        raise ValueError(f"Unknown map patch kind: {kind}")

    # Keep the original patch boundary to reduce abrupt joins.
    for c in range(width):
        cells[0][c], cells[-1][c] = border[0][c], border[-1][c]
    for r in range(height):
        cells[r][0], cells[r][-1] = border[r][0], border[r][-1]
    return tuple("".join(row) for row in cells)


def mutate_task(parent: Task, feedback: dict, rng: random.Random,
                patch_size: int, agent_rate: float,
                guidance_rate: float) -> tuple[Task, str] | None:
    if rng.random() < agent_rate:
        return mutate_agents(parent, feedback, rng, guidance_rate)
    return mutate_map(parent, feedback, rng, patch_size)


def transform(task: Task, relation: str, rng: random.Random) -> Task:
    if relation == "swap_start_goal":
        return Task(task.grid, tuple((goal, start) for start, goal in task.agents))
    if relation == "order":
        agents = list(task.agents)
        rng.shuffle(agents)
        if len(agents) > 1 and tuple(agents) == task.agents:
            agents.reverse()
        return Task(task.grid, tuple(agents))
    if relation == "left_right":
        return Task(tuple(row[::-1] for row in task.grid),
                    tuple(((s[0], task.width - 1 - s[1]), (g[0], task.width - 1 - g[1]))
                          for s, g in task.agents))
    if relation == "up_down":
        return Task(task.grid[::-1],
                    tuple(((task.height - 1 - s[0], s[1]), (task.height - 1 - g[0], g[1]))
                          for s, g in task.agents))
    if relation == "rotate_180":
        return Task(tuple(row[::-1] for row in task.grid[::-1]),
                    tuple(((task.height - 1 - s[0], task.width - 1 - s[1]),
                           (task.height - 1 - g[0], task.width - 1 - g[1]))
                          for s, g in task.agents))
    raise ValueError(relation)


def write_map(task: Task, path: Path) -> None:
    path.write_text(f"type octile\nheight {task.height}\nwidth {task.width}\nmap\n" +
                    "\n".join(task.grid) + "\n")


def write_instance(task: Task, solver: str, map_path: Path, path: Path,
                   max_timestep: int, solver_ms: int) -> None:
    if solver == "lacam3":
        lines = ["version 1"]
        for (sr, sc), (gr, gc) in task.agents:
            lines.append(f"0\t{map_path.name}\t{task.width}\t{task.height}\t"
                         f"{sc}\t{sr}\t{gc}\t{gr}\t{abs(sr-gr)+abs(sc-gc)}")
    else:
        lines = [f"map_file={map_path.name}", f"agents={len(task.agents)}", "seed=0",
                 "random_problem=0", f"max_timestep={max_timestep}",
                 f"max_comp_time={solver_ms}", ""]
        lines += [f"{sc},{sr},{gc},{gr}" for (sr, sc), (gr, gc) in task.agents]
    path.write_text("\n".join(lines) + "\n")


def parse_result(path: Path, task: Task) -> dict:
    data: dict[str, str] = {}
    frames: list[list[Cell]] = []
    in_solution = False
    for line in path.read_text().splitlines():
        if line == "solution=":
            in_solution = True
            continue
        if in_solution and re.match(r"^\d+:", line):
            frames.append([(int(y), int(x)) for x, y in COORD_RE.findall(line)])
        elif "=" in line and not in_solution:
            key, value = line.split("=", 1)
            data[key] = value
    if data.get("solved") not in {"0", "1"}:
        raise ValueError("missing or invalid solved field")
    solved = data["solved"] == "1"
    result = {"status": "solved" if solved else "unsolved",
              "reported_soc": int(data["soc"]) if "soc" in data else None,
              "reported_makespan": int(data["makespan"]) if "makespan" in data else None,
              "frames": len(frames)}
    if not solved:
        return result
    problem = validate_solution(task, frames)
    if problem:
        result["status"] = "invalid_solution"
        result["error"] = problem
    else:
        # Agents can visit a goal and later leave it to let another agent pass.
        # SOC uses the time of their final, permanent arrival.
        completion = [
            1 + max((t for t, frame in enumerate(frames) if frame[i] != goal), default=-1)
            for i, (_, goal) in enumerate(task.agents)
        ]
        result["validated_soc"] = sum(completion)
        result["agent_completion"] = completion
        result["_frames"] = frames
        if result["reported_soc"] != result["validated_soc"]:
            result["status"] = "soc_mismatch"
        usage = Counter(frames[t][i] for i, finish in enumerate(completion)
                        for t in range(finish + 1))
        result["hot_cells"] = [list(cell) for cell, _ in usage.most_common(30)]
        result["agent_waits"] = [sum(frames[t][i] == frames[t-1][i]
                                     for t in range(1, finish + 1))
                                 for i, finish in enumerate(completion)]
    return result


def validate_solution(task: Task, frames: list[list[Cell]]) -> str | None:
    if not frames or any(len(frame) != len(task.agents) for frame in frames):
        return "missing frame or wrong agent count"
    if tuple(frames[0]) != tuple(start for start, _ in task.agents):
        return "initial positions do not match"
    if tuple(frames[-1]) != tuple(goal for _, goal in task.agents):
        return "final positions do not match"
    for t, frame in enumerate(frames):
        if len(set(frame)) != len(frame):
            return f"vertex collision at time {t}"
        for r, c in frame:
            if not (0 <= r < task.height and 0 <= c < task.width) or task.grid[r][c] != ".":
                return f"obstacle or out-of-bounds position at time {t}"
        if t == 0:
            continue
        prev = frames[t - 1]
        for i, (old, new) in enumerate(zip(prev, frame)):
            if abs(old[0] - new[0]) + abs(old[1] - new[1]) > 1:
                return f"illegal move at time {t}, agent {i}"
        for i in range(len(frame)):
            for j in range(i + 1, len(frame)):
                if prev[i] == frame[j] and prev[j] == frame[i]:
                    return f"edge swap at time {t}, agents {i},{j}"
    return None


def execute(task: Task, solver: str, run_dir: Path, label: str,
            max_timestep: int, timeout: float, lacam_time: float) -> dict:
    case_dir = run_dir / label
    case_dir.mkdir(parents=True, exist_ok=True)
    result_path = case_dir / "result.txt"
    result_path.unlink(missing_ok=True)
    solver_root = LACAM if solver == "lacam3" else PIBT
    executable = solver_root / "build" / ("main" if solver == "lacam3" else "mapf")
    if not executable.is_file():
        raise FileNotFoundError(f"Solver binary missing: {executable}")
    # PIBT resolves map_file from its own map directory; LaCAM accepts an absolute path.
    with tempfile.TemporaryDirectory(prefix="mapf-repro-") as scratch:
        if solver == "lacam3":
            solver_map = Path(scratch) / "input.map"
        else:
            with tempfile.NamedTemporaryFile(prefix="repro_", suffix=".map",
                                             dir=PIBT / "map", delete=False) as handle:
                solver_map = Path(handle.name)
        try:
            write_map(task, solver_map)
            suffix = ".scen" if solver == "lacam3" else ".txt"
            input_path = Path(scratch) / f"input{suffix}"
            write_instance(task, solver, solver_map, input_path, max_timestep, int(timeout * 1000))
            # Preserve every input needed to repeat a displayed finding.
            saved_map = case_dir / "input.map"
            (saved_map).write_bytes(solver_map.read_bytes())
            saved_input = case_dir / f"input{suffix}"
            write_instance(task, solver, saved_map, saved_input, max_timestep, int(timeout * 1000))
            if solver == "lacam3":
                command = [str(executable), "-i", str(input_path), "-m", str(solver_map),
                           "-N", str(len(task.agents)), "-o", str(result_path),
                           "-t", str(lacam_time), "-v", "3", "--no-multi-thread"]
            else:
                command = [str(executable), "-i", str(input_path), "-s", solver.upper(),
                           "-o", str(result_path), "-v"]
            begin = time.monotonic()
            try:
                proc = subprocess.run(command, cwd=solver_root, capture_output=True,
                                      text=True, timeout=timeout, check=False)
                if result_path.exists():
                    try:
                        status = parse_result(result_path, task)
                    except (OSError, ValueError) as exc:
                        status = {"status": "parse_error", "error": str(exc)}
                else:
                    status = {"status": "no_result"}
                status["returncode"] = proc.returncode
                if proc.returncode != 0:
                    status["status"] = "execution_error"
                    status["error"] = proc.stderr[-1000:]
            except subprocess.TimeoutExpired:
                status = {"status": "timeout"}
            status["elapsed_seconds"] = round(time.monotonic() - begin, 3)
            return status
        finally:
            if solver != "lacam3":
                solver_map.unlink(missing_ok=True)


def run(args: argparse.Namespace) -> Path:
    rng = random.Random(args.seed)
    grid = read_map(args.map, args.size)
    if args.mode == "mutate" and args.patch_size >= min(len(grid), len(grid[0])):
        raise ValueError("Map mutation needs a local patch: set --patch-size smaller than the map")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"solver": args.solver, "map": str(args.map.resolve()),
                "size": args.size, "agents": args.agents, "seed": args.seed,
                "cases": args.cases, "threshold": args.threshold,
                "mode": args.mode, "patch_size": args.patch_size,
                "agent_mutation_rate": args.agent_mutation_rate,
                "guidance_rate": args.guidance_rate,
                "relations": ["order", "left_right", "up_down", "rotate_180",
                              "swap_start_goal"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    counts = {"completeness_failure": 0, "cost_gap": 0,
              "all_unsolved": 0, "invalid_or_error": 0, "other": 0}
    cf_count = 0
    of_count = 0
    seed_pool: list[dict] = []
    feature_vectors: list[tuple[float, ...]] = []
    second_neighbor_history: list[float] = []
    mr_violations = Counter()
    mr_quota = 5
    mr_weight_decay = 0.95
    with (out / "results.jsonl").open("w") as stream:
        for case in range(args.cases):
            parent_case = None
            mutation = "random_initialization"
            if args.mode == "mutate" and seed_pool:
                selected = select_seed(seed_pool, rng)
                parent_case, parent, feedback = selected["case"], selected["task"], selected["feedback"]
                mutated = mutate_task(parent, feedback, rng, args.patch_size,
                                      args.agent_mutation_rate, args.guidance_rate)
                if mutated is None:
                    task = generate_task(grid, args.agents, rng)
                    mutation = "random_fallback"
                    parent_case = None
                else:
                    task, mutation = mutated
            else:
                task = generate_task(grid, args.agents, rng)
            case_dir = out / f"case_{case:05d}"
            variants = {"original": task}
            for relation in manifest["relations"]:
                variants[relation] = transform(task, relation, rng)
            results = {name: execute(value, args.solver, case_dir, name,
                                     args.max_timestep, args.timeout, args.lacam_time)
                       for name, value in variants.items()}
            good = [value["reported_soc"] for value in results.values()
                    if value["status"] == "solved"]
            comparable = [value["reported_soc"] for name, value in results.items()
                          if name != "swap_start_goal" and value["status"] == "solved"]
            statuses = [value["status"] for value in results.values()]
            is_initial_seed = parent_case is None
            md = ((max(comparable) - min(comparable)) / min(comparable)) \
                if len(comparable) >= 2 and min(comparable) > 0 else None
            cf = bool(good) and any(s in ("unsolved", "timeout") for s in statuses)
            of = md is not None and md > args.threshold
            invalid_result = any(s not in ("solved", "unsolved", "timeout") for s in statuses)
            if invalid_result:
                cf = False
                of = False
            # Criticality is calculated from the original trajectory. A successful
            # original is the only kind of task that can seed the mutation pool.
            original = results["original"]
            c_score = critical_score(task, original) if original["status"] == "solved" else 0.0
            feature = seed_features(task, original) if original["status"] == "solved" else None
            divergent_mr = None
            # The artifact skips MR-weight updates when any related run is
            # unsolved (diff=None), even if the remaining solved runs show OF.
            if of and not cf and not is_initial_seed and original["status"] == "solved":
                base_soc = original["reported_soc"]
                deviations = [(abs(value["reported_soc"] - base_soc) / base_soc, name)
                              for name, value in results.items()
                              if name in ("order", "left_right", "up_down", "rotate_180")
                              and value["status"] == "solved" and base_soc]
                if deviations:
                    _, divergent_mr = max(deviations)
                    mr_violations[divergent_mr] += 1
            p_score = mr_weight_decay ** max(0, mr_violations[divergent_mr] - mr_quota) \
                if divergent_mr else 1.0
            neighbor_distances = normalized_neighbor_distances(feature, feature_vectors) \
                if feature is not None and not invalid_result else None
            distance = neighbor_distances[0] if neighbor_distances else None
            second_distance = neighbor_distances[1] if neighbor_distances else None
            s_score = diversity_weight(distance, second_neighbor_history)
            deviations = []
            base_soc = original["reported_soc"]
            if original["status"] == "solved" and base_soc:
                for name, value in results.items():
                    if name not in ("order", "left_right", "up_down", "rotate_180") \
                            or value["status"] != "solved":
                        continue
                    weight = mr_weight_decay ** max(0, mr_violations[name] - mr_quota)
                    deviations.append(abs(value["reported_soc"] - base_soc) / base_soc * weight)
            max_weighted_deviation = max(deviations, default=0.0)
            # The artifact's initial pool keeps solved OF cases but excludes
            # CF/invalid cases. Later mutated cases are accepted only if they
            # pass the failure checks.
            pool_eligible = (original["status"] == "solved" and not cf and not invalid_result
                             and (is_initial_seed or not of))
            if parent_case is None:
                # The artifact doubles the base task fitness for initial seeds.
                energy = 0.4 * c_score if original["status"] == "solved" else 0.0
            elif pool_eligible:
                energy = ((0.2 * c_score + max_weighted_deviation) * s_score *
                          length_multiplier(original["reported_soc"], len(task.agents)))
            else:
                energy = 0.0
            if second_distance is not None:
                second_neighbor_history.append(second_distance)
            feature_index_eligible = (original["status"] == "solved" and not invalid_result
                                      and (pool_eligible if is_initial_seed else True))
            if feature is not None and feature_index_eligible:
                feature_vectors.append(feature)
            if args.mode == "mutate" and pool_eligible:
                seed_pool.append({"case": case, "task": task, "feedback": original,
                                  "energy": energy, "features": feature})
            if invalid_result:
                finding = "invalid_or_error"
                cf = False
                of = False
            elif cf:
                finding = "completeness_failure"
            elif of:
                finding = "cost_gap"
            elif not good:
                finding = "all_unsolved"
            else:
                finding = "other"
            counts[finding] += 1
            cf_count += int(cf)
            of_count += int(of)
            row = {"case": case, "parent_case": parent_case,
                   "mutation": mutation, "finding": finding, "cf": cf, "of": of,
                   "md": md, "critical_score": c_score,
                   "mr_weight_of_most_divergent": p_score,
                   "nearest_feature_distance": distance, "diversity_weight": s_score,
                   "energy": energy, "violating_mr": divergent_mr,
                   "mr_violation_counts": dict(mr_violations),
                   "results": {name: {key: value for key, value in result.items()
                                      if key != "_frames"}
                               for name, result in results.items()}}
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(f"case {case}: {finding}; MD={md if md is not None else 'N/A'}; E={energy:.3f}")
    (out / "summary.json").write_text(json.dumps(
        {"counts": counts, "cf_count": cf_count, "of_count": of_count,
         "manifest": manifest}, indent=2) + "\n")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver", choices=["pibt", "hca", "pibt_plus", "lacam3"], default="pibt")
    parser.add_argument("--map", type=Path, default=PIBT / "map/empty-8-8.map")
    parser.add_argument("--size", type=int, help="nearest-neighbor resize of whole map")
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--cases", type=int, default=3)
    parser.add_argument("--mode", choices=["baseline", "mutate"], default="baseline")
    parser.add_argument("--patch-size", type=int, default=32)
    parser.add_argument("--agent-mutation-rate", type=float, default=0.5)
    parser.add_argument("--guidance-rate", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=10, help="wall-clock seconds per solver call")
    parser.add_argument("--lacam-time", type=float, default=3, help="LaCAM optimization seconds")
    parser.add_argument("--max-timestep", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=ROOT / "reproduce/output/demo")
    args = parser.parse_args()
    if args.agents < 1 or args.cases < 1 or args.timeout <= 0 or args.lacam_time < 1:
        parser.error("agents, cases and timeout must be positive; lacam-time must be at least 1 second")
    if args.patch_size < 1 or not 0 <= args.agent_mutation_rate <= 1 or not 0 <= args.guidance_rate <= 1:
        parser.error("patch-size must be positive; mutation rates must be between 0 and 1")
    print(f"Results: {run(args)}")


if __name__ == "__main__":
    main()
