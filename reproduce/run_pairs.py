"""Small, independent first stage of the MAPF-Fuzz course reproduction.

Generate MAPF instances, run one solver on symmetry-related instances, and
write replayable inputs and JSONL results. Python standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import heapq
import json
import math
import multiprocessing
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from concurrent.futures import (Future, ProcessPoolExecutor,
                                TimeoutError as FuturesTimeoutError, as_completed)
from concurrent.futures.process import BrokenProcessPool
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .checkpoint import RunJournal
else:
    from checkpoint import RunJournal


ROOT = Path(__file__).resolve().parents[1]
PIBT = ROOT / "solvers/pibt2"
LACAM = ROOT / "solvers/lacam3"
Cell = tuple[int, int]  # row, column
Pair = tuple[Cell, Cell]
COORD_RE = re.compile(r"\((\d+),(\d+)\)")  # solver writes (column,row)
SOC_RELATIONS = ("order", "left_right", "up_down", "rotate_180",
                 "add_probe", "add_obstacle")


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
    mean_agent_wait = sum(waits) / len(waits)
    waiting_long = [i for i, value in enumerate(waits)
                    if mean_agent_wait and value > 2 * mean_agent_wait]
    path_long = [i for i, (actual, distance) in enumerate(zip(travel, shortest))
                 if (distance > 0 and actual > 2 * distance)
                 or (distance == 0 and actual > 0)]
    raw = len(waiting_long) / len(waits)
    rad = len(path_long) / len(shortest)
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
    used_long = [(row, col) for row in range(task.height) for col in range(task.width)
                 if mean_use and use[(row, col)] > 2 * mean_use]
    waited_long = [(row, col) for row in range(task.height) for col in range(task.width)
                   if mean_wait and cell_wait[(row, col)] > 2 * mean_wait]
    rmu = len(used_long) / max(1, free_count)
    rmw = len(waited_long) / max(1, free_count)
    result["critical_wait_agents"] = waiting_long
    result["critical_long_agents"] = path_long
    result["critical_used_cells"] = [list(cell) for cell in used_long]
    result["critical_wait_cells"] = [list(cell) for cell in waited_long]
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


class ExactNoveltyIndex:
    """Exact standardized nearest-neighbor search for local runs."""

    def __init__(self) -> None:
        self.vectors: list[tuple[float, ...]] = []

    def query(self, features: tuple[float, ...]) -> tuple[float, float | None] | None:
        return normalized_neighbor_distances(features, self.vectors)

    def add(self, features: tuple[float, ...]) -> None:
        self.vectors.append(features)

    def build_initial(self) -> None:
        pass


class AnnoyNoveltyIndex:
    """Author-shaped Annoy index with periodically rebuilt standardized vectors."""

    def __init__(self, dimensions: int = 16, seed: int = 1,
                 n_trees: int = 50, rebuild_ratio: float = 0.1,
                 min_rebuild: int = 200, max_rebuild: int = 1000) -> None:
        try:
            from annoy import AnnoyIndex
        except ImportError as exc:
            raise RuntimeError("ANN mode needs Annoy: python3 -m pip install annoy==1.17.3") from exc
        self.factory = AnnoyIndex
        self.dimensions = dimensions
        self.seed = seed
        self.n_trees = n_trees
        self.rebuild_ratio = rebuild_ratio
        self.min_rebuild = min_rebuild
        self.max_rebuild = max_rebuild
        self.vectors: list[tuple[float, ...]] = []
        self.index = None
        self.active = False
        self.indexed_count = 0
        self.means: tuple[float, ...] = ()
        self.scales: tuple[float, ...] = ()

    def _normalize(self, vector: tuple[float, ...]) -> tuple[float, ...]:
        return tuple((value - mean) / scale for value, mean, scale
                     in zip(vector, self.means, self.scales))

    def build_initial(self) -> None:
        self.active = True
        if not self.vectors:
            return
        count = len(self.vectors)
        self.means = tuple(sum(vector[i] for vector in self.vectors) / count
                           for i in range(self.dimensions))
        self.scales = tuple(
            (sum((vector[i] - self.means[i]) ** 2 for vector in self.vectors)
             / count) ** 0.5 or 1.0 for i in range(self.dimensions))
        index = self.factory(self.dimensions, "euclidean")
        index.set_seed(self.seed)
        for i, vector in enumerate(self.vectors):
            index.add_item(i, self._normalize(vector))
        # A single build thread avoids competing with active solver workers.
        index.build(self.n_trees, n_jobs=1)
        self.index = index
        self.indexed_count = count

    def restore_indexed_prefix(self, count: int) -> None:
        """Rebuild only the last committed tree; retain newer vectors in buffer."""
        if not 0 <= count <= len(self.vectors):
            raise ValueError("Invalid Annoy indexed vector count in checkpoint")
        all_vectors = self.vectors
        try:
            self.vectors = all_vectors[:count]
            self.build_initial()
        finally:
            self.vectors = all_vectors

    def query(self, features: tuple[float, ...]) -> tuple[float, float | None] | None:
        if len(features) != self.dimensions:
            raise ValueError(f"Expected {self.dimensions} novelty features")
        if not self.vectors:
            return None
        if self.index is None:
            # Initial seeds are evaluated before the first static index build.
            return normalized_neighbor_distances(features, self.vectors)
        normalized = self._normalize(features)
        _, indexed_distances = self.index.get_nns_by_vector(
            normalized, 2, include_distances=True)
        distances = list(indexed_distances)
        # Annoy cannot insert into a built tree. Search the small pending buffer
        # exactly until the next rebuild so newly accepted cases remain visible.
        for vector in self.vectors[self.indexed_count:]:
            other = self._normalize(vector)
            distances.append(sum((a - b) ** 2 for a, b in zip(normalized, other)) ** 0.5)
        if not distances:
            return None
        distances.sort()
        return distances[0], distances[1] if len(distances) > 1 else None

    def add(self, features: tuple[float, ...]) -> None:
        if len(features) != self.dimensions:
            raise ValueError(f"Expected {self.dimensions} novelty features")
        self.vectors.append(features)
        if self.active and self.index is None:
            self.build_initial()
        elif self.index is not None:
            threshold = min(self.max_rebuild,
                            max(self.min_rebuild,
                                int(len(self.vectors) * self.rebuild_ratio)))
            if len(self.vectors) - self.indexed_count >= threshold:
                self.build_initial()


def diversity_weight(distance: float | None, history: list[float],
                     presorted: bool = False) -> float:
    """Artifact novelty buckets shared by exact and ANN search backends."""
    if distance is None:
        return 1.0
    if len(history) < 20:
        return 0.5 if distance < 0.5 else 1.5 if distance > 2.0 else 1.0
    ordered = history if presorted else sorted(history)
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


def generate_initial_tasks_per_map(maps: list[tuple[str, tuple[str, ...]]],
                                   count: int, limit: int) -> list[tuple[str, Task]]:
    """Artifact's initial generation: up to three tasks on each map, seed 42."""
    generated: list[tuple[str, Task]] = []
    for source, grid in maps:
        if len(generated) >= limit:
            break
        regions = [sorted(group) for group in components(grid)
                   if len(group) >= max(20, 2 * count)]
        if not regions:
            continue
        total = sum(len(region) for region in regions)
        ideals = [3 * len(region) / total for region in regions]
        assigned = [int(value) for value in ideals]
        remaining = 3 - sum(assigned)
        order = list(range(len(regions)))
        if remaining:
            order.sort(key=lambda i: ideals[i] - assigned[i], reverse=True)
            for i in order[:remaining]:
                assigned[i] += 1
        rng = random.Random(42)
        for i in order:
            for _ in range(assigned[i]):
                if len(generated) >= limit:
                    return generated
                endpoints = rng.sample(regions[i], 2 * count)
                agents = tuple((endpoints[j], endpoints[count + j])
                               for j in range(count))
                generated.append((source, Task(grid, agents)))
    return generated


def valid_task(task: Task) -> bool:
    """Check distinct starts/goals and connectivity, as in the author artifact."""
    starts = [start for start, _ in task.agents]
    goals = [goal for _, goal in task.agents]
    if len(set(starts)) != len(starts) or len(set(goals)) != len(goals):
        return False
    if any(not (0 <= r < task.height and 0 <= c < task.width)
           or task.grid[r][c] != "." for r, c in starts + goals):
        return False
    labels = {cell: number for number, group in enumerate(components(task.grid))
              for cell in group}
    return all(labels[start] == labels[goal] for start, goal in task.agents)


def mutate_agents(parent: Task, feedback: dict, rng: random.Random,
                  guidance_rate: float) -> tuple[Task, str] | None:
    """Choose the paper's guided or unguided agent mutation mode."""
    if rng.random() >= guidance_rate:
        return mutate_agents_unguided(parent, rng)
    return mutate_agents_guided(parent, feedback, rng)


def mutate_agents_unguided(parent: Task, rng: random.Random) -> tuple[Task, str] | None:
    """Paper's four operator families with artifact selection probabilities."""
    operation = rng.choices(
        ("agent_regenerate", "agent_swap_subset", "agent_permute_goals",
         "agent_permute_starts", "agent_relocate_endpoints"),
        weights=(0.1, 0.3, 0.2, 0.2, 0.2), k=1)[0]
    free = [(r, c) for r, line in enumerate(parent.grid)
            for c, value in enumerate(line) if value == "."]
    for _ in range(50):
        if operation == "agent_regenerate":
            try:
                candidate = generate_task(parent.grid, len(parent.agents), rng)
            except ValueError:
                return None
        else:
            agents = list(parent.agents)
            if operation == "agent_swap_subset":
                agents = [(goal, start) if rng.random() < 0.5 else (start, goal)
                          for start, goal in agents]
            elif operation == "agent_permute_goals":
                goals = [goal for _, goal in agents]
                rng.shuffle(goals)
                agents = [(agents[i][0], goals[i]) for i in range(len(agents))]
            elif operation == "agent_permute_starts":
                starts = [start for start, _ in agents]
                rng.shuffle(starts)
                agents = [(starts[i], agents[i][1]) for i in range(len(agents))]
            else:
                changed = []
                for start, goal in agents:
                    new_goal = rng.choice(free) if rng.random() < 0.2 else goal
                    new_start = rng.choice(free) if rng.random() < 0.2 else start
                    changed.append((new_start, new_goal))
                agents = changed
            candidate = Task(parent.grid, tuple(agents))
        if candidate != parent and valid_task(candidate):
            return candidate, operation
    return None


def guided_agent_operation(task: Task, operation: str, leader: int,
                           rng: random.Random) -> Task | None:
    """Move a leader locally or add a shadow/counter-flow agent nearby."""
    starts = {start for start, _ in task.agents}
    goals = {goal for _, goal in task.agents}
    occupied = starts | goals
    original_start, original_goal = task.agents[leader]
    if operation == "agent_guided_local_shift":
        start_anchor, goal_anchor = original_start, original_goal
    elif operation == "agent_guided_shadow":
        start_anchor, goal_anchor = original_start, original_goal
    elif operation == "agent_guided_counterflow":
        start_anchor, goal_anchor = original_goal, original_start
    else:
        raise ValueError(operation)
    for _ in range(100):
        start = (start_anchor[0] + rng.randrange(-5, 5),
                 start_anchor[1] + rng.randrange(-5, 5))
        goal = (goal_anchor[0] + rng.randrange(-5, 5),
                goal_anchor[1] + rng.randrange(-5, 5))
        if (start == goal or start in occupied or goal in occupied
                or not (0 <= start[0] < task.height and 0 <= start[1] < task.width)
                or not (0 <= goal[0] < task.height and 0 <= goal[1] < task.width)
                or task.grid[start[0]][start[1]] != "."
                or task.grid[goal[0]][goal[1]] != "."):
            continue
        agents = list(task.agents)
        if operation == "agent_guided_local_shift":
            agents[leader] = (start, goal)
        else:
            agents.append((start, goal))
        candidate = Task(task.grid, tuple(agents))
        if valid_task(candidate):
            return candidate
    return None


def guided_congestion_operation(task: Task, hot: Cell,
                                rng: random.Random) -> Task | None:
    """Insert an agent whose endpoints straddle a congested cell."""
    occupied = {cell for pair in task.agents for cell in pair}
    for _ in range(100):
        if rng.random() < 0.5:
            row = hot[0] + rng.randrange(-5, 5)
            if not 0 <= row < task.height:
                continue
            before = [(row, c) for c in range(hot[1])
                      if task.grid[row][c] == "." and (row, c) not in occupied]
            after = [(row, c) for c in range(hot[1] + 1, task.width)
                     if task.grid[row][c] == "." and (row, c) not in occupied]
        else:
            col = hot[1] + rng.randrange(-5, 5)
            if not 0 <= col < task.width:
                continue
            before = [(r, col) for r in range(hot[0])
                      if task.grid[r][col] == "." and (r, col) not in occupied]
            after = [(r, col) for r in range(hot[0] + 1, task.height)
                     if task.grid[r][col] == "." and (r, col) not in occupied]
        if not before or not after:
            continue
        candidate = Task(task.grid, task.agents + ((rng.choice(before), rng.choice(after)),))
        if valid_task(candidate):
            return candidate
    return None


def mutate_agents_guided(parent: Task, feedback: dict,
                         rng: random.Random) -> tuple[Task, str] | None:
    """Apply the four paper roles and restore the original agent count."""
    original_count = len(parent.agents)
    if not original_count:
        return None
    wait_agents = [i for i in feedback.get("critical_wait_agents", [])
                   if 0 <= i < original_count]
    long_agents = [i for i in feedback.get("critical_long_agents", [])
                   if 0 <= i < original_count]
    leader_weights = [1.0] * original_count
    for i in wait_agents + long_agents:
        leader_weights[i] += 2.0
    leaders = rng.choices(range(original_count), weights=leader_weights, k=10)
    current = parent
    counts = Counter()
    for leader in leaders:
        # The author's three paper-listed leader operators have weights 2:3:3.
        operation = rng.choices(
            ("agent_guided_local_shift", "agent_guided_shadow",
             "agent_guided_counterflow"), weights=(2, 3, 3), k=1)[0]
        if (operation != "agent_guided_local_shift"
                and len(current.agents) >= 2 * original_count):
            continue
        candidate = guided_agent_operation(current, operation, leader, rng)
        if candidate is not None and candidate != current:
            current = candidate
            counts[operation] += 1

    hot = [tuple(cell) for cell in
           feedback.get("critical_used_cells", []) +
           feedback.get("critical_wait_cells", [])]
    if not hot:
        hot = [tuple(cell) for cell in feedback.get("hot_cells", [])]
    if hot:
        for cell in rng.choices(hot, k=5):
            if len(current.agents) >= 2 * original_count:
                break
            candidate = guided_congestion_operation(current, cell, rng)
            if candidate is not None:
                current = candidate
                counts["agent_guided_congestion"] += 1

    added = len(current.agents) - original_count
    if added:
        # Remove distinct original agents. Repeating an index, as the artifact
        # can do, removes too few agents and changes the population size.
        removal_weights = [1.0] * original_count
        for i in wait_agents + long_agents:
            removal_weights[i] -= 0.04
        remaining = list(range(original_count))
        removed = set()
        for _ in range(added):
            index = rng.choices(remaining,
                                weights=[removal_weights[i] for i in remaining],
                                k=1)[0]
            removed.add(index)
            remaining.remove(index)
        agents = [pair for i, pair in enumerate(current.agents)
                  if i not in removed]
        current = Task(parent.grid, tuple(agents))
    if current == parent or len(current.agents) != original_count or not valid_task(current):
        return None
    names = ("local_shift", "shadow", "counterflow", "congestion")
    summary = ",".join(f"{name}={counts['agent_guided_' + name]}" for name in names)
    return current, f"agent_guided[{summary}]"


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


def trajectory_hot_scores(task: Task, result: dict) -> list[list[float]]:
    """Artifact hot map: Gaussian-weighted unvisited cells near solution paths."""
    frames = result.get("_frames", [])
    if result.get("status") != "solved" or not frames:
        raise ValueError("Probe and obstacle MRs require a solved original task")
    visited = {cell for frame in frames for cell in frame}
    goals = [goal for _, goal in task.agents]
    frequency = Counter(cell for frame in frames for i, cell in enumerate(frame)
                        if cell != goals[i])
    scores = [[0.0] * task.width for _ in range(task.height)]
    kernel = [(dr, dc, math.exp(-(dr * dr + dc * dc) / 8.0) /
               ((dr * dr + dc * dc) ** 0.5 + 1e-6))
              for dr in range(-5, 6) for dc in range(-5, 6)
              if dr or dc]
    for (row, col), count in frequency.items():
        for dr, dc, weight in kernel:
            r, c = row + dr, col + dc
            if (0 <= r < task.height and 0 <= c < task.width
                    and task.grid[r][c] == "." and (r, c) not in visited):
                scores[r][c] += count * weight
    return scores


def sample_hot_cells(scores: list[list[float]], count: int,
                     rng: random.Random) -> list[Cell]:
    """Weighted sampling without replacement via exponential-race keys."""
    candidates = ((rng.expovariate(weight), (r, c))
                  for r, row in enumerate(scores) for c, weight in enumerate(row)
                  if weight > 0)
    return [cell for _, cell in heapq.nsmallest(count, candidates)]


def transform_solution_dependent(task: Task, relation: str, rng: random.Random,
                                 scores: list[list[float]], number: int = 30) -> Task:
    """Add up to `number` one-step probes or obstacles off the original paths."""
    if relation == "add_probe":
        agents = list(task.agents)
        for start in sample_hot_cells(scores, 2 * number, rng):
            if len(agents) - len(task.agents) >= number:
                break
            row, col = start
            if scores[row][col] <= 0:
                continue
            destinations = [other for other in neighbors(task.grid, start)
                            if scores[other[0]][other[1]] > 0]
            if not destinations:
                continue
            goal = rng.choice(destinations)
            agents.append((start, goal))
            scores[row][col] = 0.0
            scores[goal[0]][goal[1]] = 0.0
        return Task(task.grid, tuple(agents))
    if relation == "add_obstacle":
        grid = [list(row) for row in task.grid]
        for row, col in sample_hot_cells(scores, number, rng):
            grid[row][col] = "@"
        return Task(tuple("".join(row) for row in grid), task.agents)
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
            # This extra copy is for our replay artifacts, not the author's
            # exec_task timer. Keep it separate from comparable execution time.
            artifact_start = time.monotonic()
            saved_map = case_dir / "input.map"
            (saved_map).write_bytes(solver_map.read_bytes())
            saved_input = case_dir / f"input{suffix}"
            write_instance(task, solver, saved_map, saved_input, max_timestep, int(timeout * 1000))
            artifact_seconds = time.monotonic() - artifact_start
            if solver == "lacam3":
                command = [str(executable), "-i", str(input_path), "-m", str(solver_map),
                           "-N", str(len(task.agents)), "-o", str(result_path),
                           "-t", str(lacam_time), "-v", "3", "--no-multi-thread"]
            else:
                command = [str(executable), "-i", str(input_path), "-s", solver.upper(),
                           "-o", str(result_path), "-v"]
            begin = time.monotonic()
            solver_seconds = 0.0
            result_parse_seconds = 0.0
            try:
                solver_start = time.monotonic()
                try:
                    proc = subprocess.run(command, cwd=solver_root, capture_output=True,
                                          text=True, timeout=timeout, check=False)
                finally:
                    solver_seconds = time.monotonic() - solver_start
                analysis_start = time.monotonic()
                if result_path.exists():
                    try:
                        status = parse_result(result_path, task)
                    except (OSError, ValueError) as exc:
                        status = {"status": "parse_error", "error": str(exc)}
                else:
                    status = {"status": "no_result"}
                result_parse_seconds = time.monotonic() - analysis_start
                status["returncode"] = proc.returncode
                if proc.returncode != 0:
                    status["status"] = "execution_error"
                    status["error"] = proc.stderr[-1000:]
            except subprocess.TimeoutExpired:
                status = {"status": "timeout"}
            status["elapsed_seconds"] = round(time.monotonic() - begin, 3)
            status["_phase_seconds"] = {
                "solver": solver_seconds,
                "result_parse": result_parse_seconds,
                "artifact_io": artifact_seconds,
            }
            return status
        finally:
            if solver != "lacam3":
                solver_map.unlink(missing_ok=True)


def evaluate_case(task: Task, solver: str, case_dir: Path, seed: int,
                  max_timestep: int, timeout: float, lacam_time: float,
                  cache_payload: bool = False,
                  fuzz_start_monotonic: float | None = None) -> dict:
    """Run a whole MR group in one worker and return compact feedback."""
    worker_start = time.monotonic()
    phase_seconds = {"metamorphic": 0.0, "execution": 0.0,
                     "solver": 0.0, "result_parse": 0.0,
                     "artifact_io": 0.0, "result_analysis": 0.0}

    def timed_execute(value: Task, label: str) -> dict:
        execution_start = time.monotonic()
        result = execute(value, solver, case_dir, label,
                         max_timestep, timeout, lacam_time)
        execution_seconds = time.monotonic() - execution_start
        timings = result.pop("_phase_seconds", {})
        artifact_io = timings.get("artifact_io", 0.0)
        phase_seconds["execution"] += max(0.0, execution_seconds - artifact_io)
        phase_seconds["solver"] += timings.get("solver", 0.0)
        phase_seconds["result_parse"] += timings.get("result_parse", 0.0)
        phase_seconds["artifact_io"] += artifact_io
        return result

    rng = random.Random(seed)
    variants = {"original": task}
    results = {"original": timed_execute(task, "original")}
    metamorphic_start = time.monotonic()
    hot_scores = (trajectory_hot_scores(task, results["original"])
                  if results["original"]["status"] == "solved" else None)
    phase_seconds["metamorphic"] += time.monotonic() - metamorphic_start
    for relation in ("swap_start_goal", "order", "left_right", "up_down",
                     "rotate_180", "add_probe", "add_obstacle"):
        metamorphic_start = time.monotonic()
        if relation in ("add_probe", "add_obstacle"):
            if hot_scores is None:
                continue
            value = transform_solution_dependent(task, relation, rng, hot_scores)
        else:
            value = transform(task, relation, rng)
        variants[relation] = value
        phase_seconds["metamorphic"] += time.monotonic() - metamorphic_start
        results[relation] = timed_execute(value, relation)
    original = results["original"]
    analysis_start = time.monotonic()
    c_score = critical_score(task, original) if original["status"] == "solved" else 0.0
    feature = seed_features(task, original) if original["status"] == "solved" else None
    phase_seconds["result_analysis"] += time.monotonic() - analysis_start
    probes_added = (len(variants["add_probe"].agents) - len(task.agents)
                    if "add_probe" in variants else None)
    obstacles_added = (sum(a == "." and b == "@"
                           for old, new in zip(task.grid, variants["add_obstacle"].grid)
                           for a, b in zip(old, new))
                       if "add_obstacle" in variants else None)
    for result in results.values():
        result.pop("_frames", None)
    worker_finished = time.monotonic()
    worker_elapsed = worker_finished - worker_start
    phase_seconds["other"] = max(
        0.0, worker_elapsed - sum(phase_seconds[name] for name in
                                ("metamorphic", "execution", "artifact_io",
                                 "result_analysis")))
    payload = {"results": results, "critical_score": c_score, "feature": feature,
               "probes_added": probes_added, "obstacles_added": obstacles_added,
               "phase_seconds": phase_seconds, "worker_elapsed_seconds": worker_elapsed,
               "worker_completed_at_utc": (datetime.now(timezone.utc)
                                           .isoformat(timespec="milliseconds")
                                           .replace("+00:00", "Z")),
               "worker_fuzz_elapsed_seconds": (
                   worker_finished - fuzz_start_monotonic
                   if fuzz_start_monotonic is not None else None)}
    if cache_payload:
        temporary = case_dir / "payload.json.tmp"
        with temporary.open("w") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, case_dir / "payload.json")
    return payload


RQ3_CATEGORIES = ("mutation", "metamorphic", "execution", "result_analysis")


def rq3_timing_summary(results_path: Path, init_cases: int,
                       wall_seconds: float | None) -> dict:
    """Summarize post-initialization times using the paper's four categories."""
    names = (*RQ3_CATEGORIES, "solver", "result_parse", "artifact_io", "other")
    totals = {name: 0.0 for name in names}
    counted = 0
    incomplete = 0
    with results_path.open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["case"] < init_cases:
                continue
            phases = row.get("phase_seconds", {})
            if not row.get("timing_complete") or "execution" not in phases:
                incomplete += 1
                continue
            counted += 1
            for name in names:
                totals[name] += phases.get(name, 0.0)
    denominator = sum(totals[name] for name in RQ3_CATEGORIES)
    return {
        "post_initialization_cases": counted,
        "excluded_incomplete_cases": incomplete,
        "seconds": {name: round(value, 6) for name, value in totals.items()},
        "categorized_seconds": round(denominator, 6),
        "category_percent": {
            name: round(100 * totals[name] / denominator, 3) if denominator else None
            for name in RQ3_CATEGORIES
        },
        "wall_seconds": round(wall_seconds, 3) if wall_seconds is not None else None,
        "wall_unattributed_seconds": (
            round(max(0.0, wall_seconds - denominator - totals["artifact_io"]), 6)
            if wall_seconds is not None else None),
        "denominator": "sum of post-initialization mutation, metamorphic, "
                       "execution and result_analysis seconds",
    }


def run(args: argparse.Namespace) -> Path:
    rng = random.Random(args.seed)
    resume_enabled = getattr(args, "resume", False)
    checkpoint_enabled = getattr(args, "checkpoint", False) or resume_enabled
    novelty_search = getattr(args, "novelty_search", "exact")
    if novelty_search not in ("exact", "annoy"):
        raise ValueError(f"Unknown novelty search backend: {novelty_search}")
    novelty_index = (ExactNoveltyIndex() if novelty_search == "exact"
                     else AnnoyNoveltyIndex(seed=args.seed))
    workers = getattr(args, "workers", 1)
    init_policy = getattr(args, "init_policy", "uniform")
    case_retention = getattr(args, "case_retention", "all")
    normal_sample_every = getattr(args, "normal_sample_every", 500)
    requested_init_cases = getattr(args, "init_cases", 1)
    time_limit = getattr(args, "time_limit", None)
    if (workers < 1 or requested_init_cases < 1 or normal_sample_every < 1
            or init_policy not in ("uniform", "per_map_three")
            or case_retention not in ("all", "failures_samples")
            or (time_limit is not None and time_limit <= 0)):
        raise ValueError("invalid worker, initial seed, retention or time-limit settings")
    map_dir = getattr(args, "map_dir", None)
    map_paths = sorted(map_dir.glob("*.map")) if map_dir else [args.map]
    if not map_paths:
        raise ValueError(f"No .map files found in {map_dir}")
    maps = [(str(path.resolve()), read_map(path, args.size)) for path in map_paths]
    initial_tasks = (generate_initial_tasks_per_map(maps, args.agents,
                                                    min(args.cases, requested_init_cases))
                     if init_policy == "per_map_three" else [])
    init_cases = (min(args.cases, requested_init_cases, len(initial_tasks))
                  if init_policy == "per_map_three"
                  else min(args.cases, requested_init_cases))
    if init_cases == 0:
        raise ValueError("No eligible initial tasks in the selected maps")
    if args.mode == "mutate" and any(
            args.patch_size >= min(len(grid), len(grid[0])) for _, grid in maps):
        raise ValueError("Map mutation needs a local patch: set --patch-size smaller than the map")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if checkpoint_enabled and not resume_enabled and (out / "manifest.json").exists():
        raise FileExistsError(f"Output already has a run; choose another directory: {out}")
    solver_binary = ((LACAM if args.solver == "lacam3" else PIBT) / "build" /
                     ("main" if args.solver == "lacam3" else "mapf"))
    source_files = (Path(__file__), Path(__file__).with_name("checkpoint.py"))
    manifest = {"solver": args.solver, "map": str(map_paths[0].resolve()) if len(maps) == 1 else None,
                "maps": [path for path, _ in maps],
                "map_sha256": {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in map_paths},
                "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in source_files},
                "solver_sha256": (hashlib.sha256(solver_binary.read_bytes()).hexdigest()
                                  if solver_binary.is_file() else None),
                "size": args.size, "agents": args.agents, "seed": args.seed,
                "cases": args.cases, "workers": workers,
                "init_cases": init_cases, "init_cases_requested": requested_init_cases,
                "init_policy": init_policy, "time_limit": time_limit,
                "case_retention": case_retention,
                "novelty_search": novelty_search,
                "checkpoint": checkpoint_enabled,
                "timeout": args.timeout,
                "lacam_time": args.lacam_time,
                "max_timestep": args.max_timestep,
                "normal_sample_every": normal_sample_every,
                "threshold": args.threshold,
                "mode": args.mode, "patch_size": args.patch_size,
                "agent_mutation_rate": args.agent_mutation_rate,
                "guidance_rate": args.guidance_rate,
                "relations": ["swap_start_goal", "order", "left_right", "up_down",
                              "rotate_180", "add_probe", "add_obstacle"],
                "soc_relations": list(SOC_RELATIONS),
                "solution_dependent_relations": ["add_probe", "add_obstacle"],
                "probe_and_obstacle_target": 30}
    journal = (RunJournal(out, manifest, resume_enabled, rng.getstate())
               if checkpoint_enabled else None)
    if journal is not None:
        if resume_enabled:
            journal.sync_outputs()
            if journal.meta("final_status") in ("case_limit", "time_limit"):
                summary = journal.meta("final_summary")
                if summary is not None:
                    temporary = out / "summary.json.tmp"
                    temporary.write_text(json.dumps(summary, indent=2) + "\n")
                    os.replace(temporary, out / "summary.json")
                journal.close()
                return out
            rng.setstate(journal.meta("rng_state"))
            for vector in journal.load_features():
                novelty_index.add(vector)
            if journal.meta("phase_started"):
                if novelty_search == "annoy":
                    novelty_index.restore_indexed_prefix(
                        journal.meta("annoy_indexed_count", 0))
                else:
                    novelty_index.build_initial()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    counts = (journal.meta("counts") if resume_enabled else
              {"completeness_failure": 0, "cost_gap": 0,
               "all_unsolved": 0, "invalid_or_error": 0, "other": 0})
    cf_count = journal.meta("cf_count") if resume_enabled else 0
    of_count = journal.meta("of_count") if resume_enabled else 0
    seed_pool: list[dict] = journal.load_seed_pool() if resume_enabled else []
    second_neighbor_history: list[float] = journal.load_history() if resume_enabled else []
    mr_violations = Counter(journal.meta("mr_violations")) if resume_enabled else Counter()
    mr_quota = 5
    mr_weight_decay = 0.95
    def process_complete(case: int, parent_case: int | None, task: Task,
                         map_source: str,
                         mutation: str, payload: dict) -> tuple:
        nonlocal cf_count, of_count
        results = payload["results"]
        c_score = payload["critical_score"]
        feature = payload["feature"]
        probes_added = payload["probes_added"]
        obstacles_added = payload["obstacles_added"]
        good = [value["reported_soc"] for value in results.values()
                if value["status"] == "solved"]
        comparable = [value["reported_soc"] for name, value in results.items()
                      if name in SOC_RELATIONS or name == "original"
                      if value["status"] == "solved"]
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
        divergent_mr = None
        # The artifact skips MR-weight updates when any related run is
        # unsolved (diff=None), even if the remaining solved runs show OF.
        if of and not cf and not is_initial_seed and original["status"] == "solved":
            base_soc = original["reported_soc"]
            deviations = [(abs(value["reported_soc"] - base_soc) / base_soc, name)
                          for name, value in results.items()
                          if name in SOC_RELATIONS
                          and value["status"] == "solved" and base_soc]
            if deviations:
                _, divergent_mr = max(deviations)
                mr_violations[divergent_mr] += 1
        p_score = mr_weight_decay ** max(0, mr_violations[divergent_mr] - mr_quota) \
            if divergent_mr else 1.0
        neighbor_distances = novelty_index.query(feature) \
            if feature is not None and not invalid_result else None
        distance = neighbor_distances[0] if neighbor_distances else None
        second_distance = neighbor_distances[1] if neighbor_distances else None
        s_score = diversity_weight(distance, second_neighbor_history, presorted=True)
        deviations = []
        base_soc = original.get("reported_soc")
        if original["status"] == "solved" and base_soc:
            for name, value in results.items():
                if name not in SOC_RELATIONS \
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
            bisect.insort(second_neighbor_history, second_distance)
        feature_index_eligible = (original["status"] == "solved" and not invalid_result
                                  and (pool_eligible if is_initial_seed else True))
        if feature is not None and feature_index_eligible:
            novelty_index.add(feature)
        if args.mode == "mutate" and pool_eligible:
            seed_pool.append({"case": case, "task": task, "feedback": original,
                              "energy": energy, "features": feature,
                              "map_source": map_source})
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
        keep_case_files = (case_retention == "all" or
                           finding in ("completeness_failure", "cost_gap",
                                       "invalid_or_error") or
                           (finding == "other" and case % normal_sample_every == 0))
        counts[finding] += 1
        cf_count += int(cf)
        of_count += int(of)
        probe_soc_above_baseline = None
        if (probes_added is not None and results["original"]["status"] == "solved"
                and results["add_probe"]["status"] == "solved"):
            probe_soc_above_baseline = (results["add_probe"]["reported_soc"]
                                        - results["original"]["reported_soc"]
                                        - probes_added)
        row = {"case": case, "parent_case": parent_case,
               "map_source": map_source,
               "case_files_selected_for_retention": keep_case_files,
               "mutation": mutation, "finding": finding, "cf": cf, "of": of,
               "relation_changes": {
                   "probes_added": probes_added,
                   "obstacles_added": obstacles_added,
                   "probe_soc_above_original_plus_probe_steps": probe_soc_above_baseline,
               },
               "md": md, "critical_score": c_score,
               "mr_weight_of_most_divergent": p_score,
               "nearest_feature_distance": distance, "diversity_weight": s_score,
               "energy": energy, "violating_mr": divergent_mr,
               "mr_violation_counts": dict(mr_violations),
               "results": {name: {key: value for key, value in result.items()
                                  if key != "_frames"}
                           for name, result in results.items()}}
        return (row, keep_case_files, feature, feature_index_eligible,
                pool_eligible, second_distance, original)
    completed = journal.completed_count() if resume_enabled else 0
    next_case = journal.meta("next_case") if resume_enabled else 0
    init_completed = journal.meta("init_completed") if resume_enabled else 0
    saved_pending = journal.load_pending() if resume_enabled else []
    cached_pending_payloads = {}
    for saved in saved_pending:
        cached = out / f"case_{saved['case']:05d}" / "payload.json"
        if cached.is_file():
            cached_pending_payloads[saved["case"]] = json.loads(cached.read_text())
    resume_elapsed = (journal.meta("fuzz_elapsed")
                      if resume_enabled and journal.meta("phase_started") else None)
    if resume_elapsed is not None:
        for cached_payload in cached_pending_payloads.values():
            cached_elapsed = cached_payload.get("worker_fuzz_elapsed_seconds")
            if isinstance(cached_elapsed, (int, float)) and math.isfinite(cached_elapsed):
                resume_elapsed = max(resume_elapsed, cached_elapsed)
    fuzz_start = (time.monotonic() - resume_elapsed
                  if resume_elapsed is not None else None)
    dispatch_error = None
    processing_error = None
    pool_context = (ProcessPoolExecutor(max_workers=workers,
                                        mp_context=multiprocessing.get_context("fork"))
                    if workers > 1 else nullcontext(None))
    with (out / "results.jsonl").open("a" if resume_enabled else "w") as stream, pool_context as executor:
        pending: dict[Future, tuple[int, int | None, Task, str, str, float]] = {}
        recovering_pending = bool(saved_pending)
        if resume_enabled:
            for saved in saved_pending:
                case = saved["case"]
                task = saved["task"]
                case_dir = out / f"case_{case:05d}"
                if case in cached_pending_payloads:
                    future = Future()
                    future.set_result(cached_pending_payloads[case])
                else:
                    arguments = (task, args.solver, case_dir, saved["case_seed"],
                                 args.max_timestep, args.timeout, args.lacam_time,
                                 True, fuzz_start)
                    if executor is None:
                        future = Future()
                        try:
                            future.set_result(evaluate_case(*arguments))
                        except BaseException as exc:
                            future.set_exception(exc)
                    else:
                        try:
                            future = executor.submit(evaluate_case, *arguments)
                        except Exception as exc:
                            dispatch_error = repr(exc)
                            break
                pending[future] = (case, saved["parent_case"], task,
                                   saved["map_source"], saved["mutation"],
                                   saved["mutation_seconds"])
        while next_case < args.cases or pending:
            if processing_error is not None or dispatch_error is not None:
                break
            if init_completed == init_cases and fuzz_start is None:
                try:
                    novelty_index.build_initial()
                    if journal is not None:
                        journal.start_fuzzing(
                            novelty_index.indexed_count if novelty_search == "annoy" else 0)
                except Exception as exc:
                    processing_error = f"initial novelty index build: {exc!r}"
                    break
                fuzz_start = time.monotonic()
            while (len(pending) < workers and next_case < args.cases
                   and dispatch_error is None and not recovering_pending):
                if next_case >= init_cases and init_completed < init_cases:
                    break  # Complete initial pool before starting the timed fuzzing phase.
                if (fuzz_start is not None and time_limit is not None
                        and time.monotonic() - fuzz_start >= time_limit):
                    break
                case = next_case
                mutation_start = time.monotonic()
                parent_case = None
                mutation = "random_initialization"
                selected = None
                if args.mode == "mutate" and case >= init_cases and seed_pool:
                    selected = select_seed(seed_pool, rng)
                    parent_case = selected["case"]
                    parent = selected["task"]
                    feedback = selected["feedback"]
                    map_source = selected["map_source"]
                    mutated = mutate_task(parent, feedback, rng, args.patch_size,
                                          args.agent_mutation_rate, args.guidance_rate)
                    if mutated is None:
                        map_source, grid = rng.choice(maps)
                        task = generate_task(grid, args.agents, rng)
                        mutation = "random_fallback"
                        parent_case = None
                    else:
                        task, mutation = mutated
                else:
                    if case < init_cases and init_policy == "per_map_three":
                        map_source, task = initial_tasks[case]
                    else:
                        map_source, grid = rng.choice(maps)
                        task = generate_task(grid, args.agents, rng)
                case_dir = out / f"case_{case:05d}"
                case_seed = rng.randrange(2**63)
                mutation_seconds = time.monotonic() - mutation_start
                if journal is not None:
                    try:
                        journal.dispatch(case, parent_case, task, map_source,
                                         mutation, case_seed, rng.getstate(), selected,
                                         time.monotonic() - fuzz_start
                                         if fuzz_start is not None else 0.0,
                                         mutation_seconds)
                    except Exception as exc:
                        processing_error = f"case {case} checkpoint dispatch: {exc!r}"
                        break
                arguments = (task, args.solver, case_dir, case_seed,
                             args.max_timestep, args.timeout, args.lacam_time,
                             journal is not None, fuzz_start)
                if executor is None:
                    future = Future()
                    try:
                        future.set_result(evaluate_case(*arguments))
                    except BaseException as exc:
                        future.set_exception(exc)
                else:
                    try:
                        future = executor.submit(evaluate_case, *arguments)
                    except Exception as exc:
                        dispatch_error = repr(exc)
                        break  # The checkpointed case remains pending for resume.
                pending[future] = (case, parent_case, task, map_source,
                                   mutation, mutation_seconds)
                next_case += 1
            if processing_error is not None:
                break
            if not pending:
                break
            try:
                future = next(as_completed(pending, timeout=5))
            except FuturesTimeoutError:
                if journal is not None and fuzz_start is not None:
                    try:
                        journal.update_elapsed(time.monotonic() - fuzz_start)
                    except Exception as exc:
                        processing_error = f"checkpoint heartbeat: {exc!r}"
                        break
                continue
            case, parent_case, task, map_source, mutation, mutation_seconds = pending.pop(future)
            try:
                payload = future.result()
            except BrokenProcessPool as exc:
                dispatch_error = repr(exc)
                break  # Keep all uncommitted cases in SQLite for the next run.
            except Exception as exc:
                # A failed case is recorded without losing other completed work.
                payload = {"results": {"original": {
                    "status": "execution_error", "error": repr(exc)}},
                    "critical_score": 0.0, "feature": None,
                    "probes_added": None, "obstacles_added": None}
            try:
                scoring_start = time.monotonic()
                row, keep_files, feature, feature_eligible, pool_eligible, \
                    second_distance, feedback = process_complete(
                        case, parent_case, task, map_source, mutation, payload)
                scoring_seconds = time.monotonic() - scoring_start
                worker_phases = payload.get("phase_seconds") or {}
                row["timing_complete"] = bool(worker_phases)
                row["phase_seconds"] = {
                    "mutation": round(mutation_seconds, 6),
                    "metamorphic": round(worker_phases.get("metamorphic", 0.0), 6),
                    "execution": round(worker_phases.get("execution", 0.0), 6),
                    "solver": round(worker_phases.get("solver", 0.0), 6),
                    "result_parse": round(worker_phases.get("result_parse", 0.0), 6),
                    "artifact_io": round(worker_phases.get("artifact_io", 0.0), 6),
                    "result_analysis": round(
                        worker_phases.get("result_analysis", 0.0) + scoring_seconds, 6),
                    "other": round(worker_phases.get("other", 0.0), 6),
                }
                row["worker_elapsed_seconds"] = (
                    round(payload["worker_elapsed_seconds"], 6)
                    if "worker_elapsed_seconds" in payload else None)
                worker_fuzz_elapsed = payload.get("worker_fuzz_elapsed_seconds")
                row["fuzz_elapsed_seconds"] = (
                    round(worker_fuzz_elapsed, 3)
                    if worker_fuzz_elapsed is not None else
                    round(time.monotonic() - fuzz_start, 3)
                    if fuzz_start is not None else None)
                row["completed_at_utc"] = payload.get("worker_completed_at_utc") or (
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"))
                next_init_completed = init_completed + int(case < init_cases)
                if journal is not None:
                    journal.complete(
                        case, row, keep_files, task, feedback, feature,
                        feature_eligible, pool_eligible and args.mode == "mutate",
                        second_distance, counts, cf_count, of_count,
                        dict(mr_violations), next_init_completed,
                        time.monotonic() - fuzz_start if fuzz_start is not None else 0.0,
                        novelty_index.indexed_count if novelty_search == "annoy" else 0)
                    completed += 1
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                if journal is None:
                    completed += 1
                if not keep_files:
                    shutil.rmtree(out / f"case_{case:05d}")
                print(f"case {case}: {row['finding']}; "
                      f"MD={row['md'] if row['md'] is not None else 'N/A'}; "
                      f"E={row['energy']:.3f}")
            except Exception as exc:
                processing_error = f"case {case} processing: {exc!r}"
                break
            init_completed = next_init_completed
            if recovering_pending and not pending:
                recovering_pending = False
    if journal is not None and processing_error is not None:
        # Scoring may have changed in-memory state before its transaction failed.
        counts = journal.meta("counts")
        cf_count = journal.meta("cf_count")
        of_count = journal.meta("of_count")
        completed = journal.completed_count()
    stop_reason = ("processing_error" if processing_error is not None else
                   "worker_pool_error" if dispatch_error is not None else
                   "time_limit" if completed < args.cases else "case_limit")
    final_fuzz_elapsed = (time.monotonic() - fuzz_start
                          if fuzz_start is not None else None)
    summary = {"counts": counts, "cf_count": cf_count, "of_count": of_count,
               "completed_cases": completed, "requested_cases": args.cases,
               "fuzz_elapsed_seconds": (round(final_fuzz_elapsed, 3)
                                        if final_fuzz_elapsed is not None else None),
               "stop_reason": stop_reason,
               "dispatch_error": dispatch_error,
               "processing_error": processing_error,
               "manifest": manifest}
    if workers == 1 and time_limit is not None:
        summary["rq3_timing"] = rq3_timing_summary(
            out / "results.jsonl", init_cases, final_fuzz_elapsed)
    if journal is not None:
        if stop_reason in ("case_limit", "time_limit"):
            journal.finish(stop_reason, final_fuzz_elapsed or 0.0, summary)
        journal.close()
    temporary = out / "summary.json.tmp"
    temporary.write_text(json.dumps(summary, indent=2) + "\n")
    os.replace(temporary, out / "summary.json")
    if processing_error is not None:
        raise RuntimeError(f"Experiment stopped early: {processing_error}; see {out / 'summary.json'}")
    if dispatch_error is not None:
        raise RuntimeError(f"Worker pool stopped early: {dispatch_error}; see {out / 'summary.json'}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver", choices=["pibt", "hca", "pibt_plus", "lacam3"], default="pibt")
    parser.add_argument("--map", type=Path, default=PIBT / "map/empty-8-8.map")
    parser.add_argument("--map-dir", type=Path,
                        help="sample initial seeds from every .map in this directory")
    parser.add_argument("--size", type=int, help="nearest-neighbor resize of whole map")
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--cases", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--init-cases", type=int, default=1)
    parser.add_argument("--init-policy", choices=("uniform", "per_map_three"),
                        default="uniform")
    parser.add_argument("--case-retention", choices=("all", "failures_samples"),
                        default="all")
    parser.add_argument("--normal-sample-every", type=int, default=500)
    parser.add_argument("--novelty-search", choices=("exact", "annoy"),
                        default="exact")
    parser.add_argument("--time-limit", type=float,
                        help="seconds of fuzzing after the initial seed phase")
    parser.add_argument("--checkpoint", action="store_true",
                        help="persist each dispatch and completed group for recovery")
    parser.add_argument("--resume", action="store_true",
                        help="continue an interrupted --checkpoint run in --output")
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
    if (args.agents < 1 or args.cases < 1 or args.workers < 1
            or args.init_cases < 1 or args.normal_sample_every < 1
            or args.timeout <= 0 or args.lacam_time < 1
            or (args.time_limit is not None and args.time_limit <= 0)):
        parser.error("agents, cases and timeout must be positive; lacam-time must be at least 1 second")
    if args.patch_size < 1 or not 0 <= args.agent_mutation_rate <= 1 or not 0 <= args.guidance_rate <= 1:
        parser.error("patch-size must be positive; mutation rates must be between 0 and 1")
    print(f"Results: {run(args)}")


if __name__ == "__main__":
    main()
