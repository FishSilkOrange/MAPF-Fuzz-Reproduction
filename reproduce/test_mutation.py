"""Property checks for the first mutation stage (no solver required)."""

import random
import tempfile
import unittest
from pathlib import Path

from reproduce.run_pairs import (Task, diversity_weight, mutate_agents, mutate_map,
                                 length_multiplier, normalized_nearest_distance,
                                 read_map, seed_features, select_seed, valid_task)


class MutationChecks(unittest.TestCase):
    def test_resize_uses_whole_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.map"
            path.write_text("type octile\nheight 4\nwidth 4\nmap\n"
                            "...@\n@...\n..@@\n@@@@\n")
            self.assertEqual(read_map(path, 2), ("..", ".@"))

    def test_agent_mutation_preserves_task_validity(self):
        parent = Task(("........",) * 8,
                      (((0, 0), (7, 7)), ((0, 7), (7, 0))))
        result = mutate_agents(parent, {"agent_waits": [10, 0]}, random.Random(2), 1.0)
        self.assertIsNotNone(result)
        child, operation = result
        self.assertEqual(operation, "agent_guided_local_shift")
        self.assertEqual(child.grid, parent.grid)
        self.assertEqual(len(child.agents), len(parent.agents))
        self.assertTrue(valid_task(child))

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
        self.assertAlmostEqual(length_multiplier(0, 1), 0.0886, places=3)

    def test_parent_selection_uses_max_fitness_and_decay(self):
        low = {"energy": 1.0}
        high = {"energy": 10.0}
        self.assertIs(select_seed([low, high], random.Random(1)), high)
        self.assertEqual(high["energy"], 9.0)


if __name__ == "__main__":
    unittest.main()
