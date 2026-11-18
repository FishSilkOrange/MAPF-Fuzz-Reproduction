"""Property checks for the first mutation stage (no solver required)."""

import random
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from reproduce.run_pairs import (AnnoyNoveltyIndex, ExactNoveltyIndex, Task,
                                 diversity_weight, generate_initial_tasks_per_map,
                                 guided_agent_operation,
                                 guided_congestion_operation, mutate_agents,
                                 mutate_agents_unguided, mutate_map,
                                 length_multiplier, normalized_nearest_distance,
                                 read_map, seed_features, select_seed, transform,
                                 trajectory_hot_scores, transform_solution_dependent,
                                 valid_task, validate_solution)


class MutationChecks(unittest.TestCase):
    def test_novelty_backends_share_distance_contract(self):
        class FakeAnnoyIndex:
            def __init__(self, dimensions, metric):
                self.vectors = {}
                self.metric = metric

            def set_seed(self, seed):
                self.seed = seed

            def add_item(self, index, vector):
                self.vectors[index] = tuple(vector)

            def build(self, trees, n_jobs=1):
                self.trees = trees

            def get_nns_by_vector(self, vector, count, include_distances=False):
                pairs = sorted((math.dist(vector, other), index)
                               for index, other in self.vectors.items())[:count]
                return [index for _, index in pairs], [distance for distance, _ in pairs]

        exact = ExactNoveltyIndex()
        with patch.dict(sys.modules, {"annoy": types.SimpleNamespace(AnnoyIndex=FakeAnnoyIndex)}):
            ann = AnnoyNoveltyIndex(dimensions=2, seed=7, n_trees=3,
                                    min_rebuild=2, max_rebuild=2)
        for vector in ((0.0, 0.0), (2.0, 0.0)):
            exact.add(vector)
            ann.add(vector)
        self.assertEqual(exact.query((2.0, 0.0)), ann.query((2.0, 0.0)))
        ann.build_initial()
        self.assertEqual(ann.indexed_count, 2)
        self.assertEqual(ann.query((2.0, 0.0))[0], 0.0)
        ann.add((10.0, 10.0))
        self.assertEqual(ann.indexed_count, 2)
        self.assertEqual(ann.query((10.0, 10.0))[0], 0.0)
        with patch.dict(sys.modules, {"annoy": types.SimpleNamespace(AnnoyIndex=FakeAnnoyIndex)}):
            restored = AnnoyNoveltyIndex(dimensions=2, seed=7, n_trees=3,
                                         min_rebuild=2, max_rebuild=2)
        for vector in ann.vectors:
            restored.add(vector)
        restored.restore_indexed_prefix(ann.indexed_count)
        self.assertEqual(restored.indexed_count, 2)
        self.assertEqual(restored.query((10.0, 10.0)), ann.query((10.0, 10.0)))
        ann.add((20.0, 20.0))
        self.assertEqual(ann.indexed_count, 4)
        self.assertEqual(ann.query((20.0, 20.0))[0], 0.0)
        with patch.dict(sys.modules, {"annoy": types.SimpleNamespace(AnnoyIndex=FakeAnnoyIndex)}):
            empty_initial = AnnoyNoveltyIndex(dimensions=2)
        empty_initial.build_initial()
        empty_initial.add((1.0, 1.0))
        self.assertEqual(empty_initial.indexed_count, 1)

    def test_author_initialization_generates_at_most_three_per_map(self):
        grid = (".....@....",) * 10
        maps = [("map_a", grid), ("map_b", grid)]
        generated = generate_initial_tasks_per_map(maps, 2, 100)
        self.assertEqual(len(generated), 6)
        self.assertEqual([source for source, _ in generated],
                         ["map_a"] * 3 + ["map_b"] * 3)
        self.assertEqual(generated, generate_initial_tasks_per_map(maps, 2, 100))
        self.assertEqual([task.agents[0][0][1] < 5 for _, task in generated[:3]],
                         [True, True, False])
        self.assertTrue(all(valid_task(task) for _, task in generated))

    def test_resize_uses_whole_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.map"
            path.write_text("type octile\nheight 4\nwidth 4\nmap\n"
                            "...@\n@...\n..@@\n@@@@\n")
            self.assertEqual(read_map(path, 2), ("..", ".@"))

    def test_agent_mutation_preserves_task_validity(self):
        parent = Task(("." * 32,) * 32,
                      tuple(((i + 1, 1), (30 - i, 30)) for i in range(12)))
        feedback = {"critical_wait_agents": [0, 1],
                    "critical_long_agents": [1, 2],
                    "critical_used_cells": [[16, 16]],
                    "critical_wait_cells": [[15, 16]]}
        result = mutate_agents(parent, feedback, random.Random(2), 1.0)
        self.assertIsNotNone(result)
        child, operation = result
        self.assertRegex(operation, r"^agent_guided\[local_shift=\d+,shadow=\d+,"
                                    r"counterflow=\d+,congestion=\d+\]$")
        self.assertEqual(child.grid, parent.grid)
        self.assertEqual(len(child.agents), len(parent.agents))
        self.assertTrue(valid_task(child))

    def test_all_four_guided_roles_create_valid_candidates(self):
        parent = Task(("." * 32,) * 32,
                      (((8, 8), (24, 24)), ((8, 24), (24, 8))))
        for operation in ("agent_guided_local_shift", "agent_guided_shadow",
                          "agent_guided_counterflow"):
            with self.subTest(operation=operation):
                candidate = guided_agent_operation(parent, operation, 0,
                                                   random.Random(12))
                self.assertIsNotNone(candidate)
                self.assertTrue(valid_task(candidate))
                self.assertEqual(len(candidate.agents), len(parent.agents) +
                                 (operation != "agent_guided_local_shift"))
        congestion = guided_congestion_operation(parent, (16, 16), random.Random(3))
        self.assertIsNotNone(congestion)
        self.assertTrue(valid_task(congestion))
        self.assertEqual(len(congestion.agents), len(parent.agents) + 1)
        start, goal = congestion.agents[-1]
        self.assertTrue((start[0] == goal[0] and start[1] < 16 < goal[1])
                        or (start[1] == goal[1] and start[0] < 16 < goal[0]))

    def test_unguided_agent_operators_keep_population_and_validity(self):
        parent = Task(("." * 8,) * 8,
                      (((0, 0), (7, 7)), ((0, 7), (7, 0)),
                       ((1, 0), (6, 7))))
        operations = set()
        for seed in range(60):
            result = mutate_agents_unguided(parent, random.Random(seed))
            self.assertIsNotNone(result)
            child, operation = result
            operations.add(operation)
            self.assertEqual(len(child.agents), len(parent.agents))
            self.assertTrue(valid_task(child))
        self.assertEqual(operations, {
            "agent_regenerate", "agent_swap_subset", "agent_permute_goals",
            "agent_permute_starts", "agent_relocate_endpoints"})

    def test_valid_task_allows_start_goal_cross_overlap(self):
        task = Task(("....",) * 4,
                    (((0, 0), (3, 3)), ((3, 3), (0, 0))))
        self.assertTrue(valid_task(task))

    def test_map_mutation_is_local_and_preserves_endpoints(self):
        parent = Task(("." * 16,) * 16,
                      (((0, 0), (15, 15)), ((0, 15), (15, 0))))
        result = mutate_map(parent, {"hot_cells": [[8, 8], [9, 8]]}, random.Random(3), 4)
        self.assertIsNotNone(result)
        child, operation = result
        self.assertRegex(operation, r"^map_(uniform|ca|dla)(_(uniform|ca|dla))*_patch_x[1-3]$")
        self.assertEqual(child.agents, parent.agents)
        self.assertTrue(valid_task(child))
        changed = sum(a != b for old, new in zip(parent.grid, child.grid)
                      for a, b in zip(old, new))
        self.assertGreater(changed, 0)
        self.assertLessEqual(changed, 4 * 4)

    def test_map_mutation_can_apply_multiple_hotspot_patches(self):
        parent = Task(("." * 32,) * 32,
                      (((0, 0), (31, 31)), ((0, 31), (31, 0))))
        result = mutate_map(parent,
                            {"hot_cells": [[8, 8], [16, 8], [24, 24]]},
                            random.Random(5), 4)
        self.assertIsNotNone(result)
        child, operation = result
        self.assertRegex(operation, r"_patch_x[2-3]$")
        self.assertTrue(valid_task(child))

    def test_seed_features_and_diversity_scores(self):
        task = Task(("....", ".@..", "....", "...."),
                    (((0, 0), (3, 3)), ((0, 3), (3, 0))))
        result = {"agent_waits": [2, 0], "agent_completion": [8, 6]}
        vector = seed_features(task, result)
        self.assertEqual(len(vector), 16)
        pool = [vector]
        self.assertEqual(normalized_nearest_distance(vector, pool), 0.0)
        self.assertEqual(diversity_weight(0.4, [0.2, 0.4, 0.6, 0.8]), 0.5)
        self.assertEqual(diversity_weight(2.1, [0.2, 0.4, 0.6, 0.8]), 1.5)
        values = list(reversed([float(i) for i in range(20)]))
        self.assertEqual(diversity_weight(7.0, values),
                         diversity_weight(7.0, sorted(values), presorted=True))
        self.assertAlmostEqual(length_multiplier(0, 1), 0.0886, places=3)

    def test_parent_selection_uses_max_fitness_and_decay(self):
        low = {"energy": 1.0}
        high = {"energy": 10.0}
        self.assertIs(select_seed([low, high], random.Random(1)), high)
        self.assertEqual(high["energy"], 9.0)

    def test_order_relation_permutes_agents(self):
        task = Task(("....",) * 4,
                    (((0, 0), (3, 3)), ((0, 3), (3, 0)), ((1, 0), (2, 3))))
        transformed = transform(task, "order", random.Random(1))
        self.assertCountEqual(transformed.agents, task.agents)
        self.assertNotEqual(transformed.agents, task.agents)

    def test_solution_dependent_relations_preserve_original_path(self):
        task = Task(("." * 16,) * 16, (((8, 0), (8, 15)),))
        frames = [[(8, col)] for col in range(16)]
        scores = trajectory_hot_scores(task, {"status": "solved", "_frames": frames})
        probes = transform_solution_dependent(task, "add_probe", random.Random(1), scores, 3)
        obstacles = transform_solution_dependent(task, "add_obstacle", random.Random(2), scores, 3)
        path = {frame[0] for frame in frames}
        self.assertGreater(len(probes.agents), len(task.agents))
        self.assertLessEqual(len(probes.agents), len(task.agents) + 3)
        self.assertTrue(all(start not in path and goal not in path
                            for start, goal in probes.agents[1:]))
        self.assertTrue(valid_task(probes))
        self.assertTrue(all(obstacles.grid[row][col] == "." for row, col in path))
        self.assertTrue(valid_task(obstacles))
        probe_frames = [frame + [start if t == 0 else goal
                                 for start, goal in probes.agents[1:]]
                        for t, frame in enumerate(frames)]
        self.assertIsNone(validate_solution(probes, probe_frames))
        self.assertIsNone(validate_solution(obstacles, frames))


if __name__ == "__main__":
    unittest.main()
