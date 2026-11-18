"""Recovery checks that do not need a solver or the Annoy extension."""

import io
import json
import multiprocessing
import random
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reproduce.checkpoint import RunJournal
from reproduce import run_pairs, run_rq3, run_server


class CheckpointChecks(unittest.TestCase):
    def test_run_lock_blocks_concurrent_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"solver": "pibt"}
            journal = RunJournal(root, manifest, False, random.Random(1).getstate())
            with self.assertRaisesRegex(RuntimeError, "still use"):
                RunJournal(root, manifest, True)
            journal.close()
            resumed = RunJournal(root, manifest, True)
            resumed.close()

    def test_forked_worker_keeps_lock_after_parent_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"solver": "pibt"}
            journal = RunJournal(root, manifest, False, random.Random(1).getstate())
            context = multiprocessing.get_context("fork")
            started = context.Event()
            release = context.Event()

            def hold_inherited_descriptor():
                started.set()
                release.wait(5)

            worker = context.Process(target=hold_inherited_descriptor)
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                journal.close()
                with self.assertRaisesRegex(RuntimeError, "still use"):
                    RunJournal(root, manifest, True)
            finally:
                release.set()
                worker.join(5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            resumed = RunJournal(root, manifest, True)
            resumed.close()

    def test_pending_transaction_and_jsonl_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"solver": "pibt", "cases": 2}
            state = random.Random(7).getstate()
            journal = RunJournal(root, manifest, False, state)
            journal.dispatch(0, None, ("task",), "sample.map", "initial", 17,
                             state, None, 0.0, 0.123)
            journal.close()

            resumed = RunJournal(root, manifest, True)
            self.assertEqual(resumed.meta("next_case"), 1)
            self.assertEqual(resumed.meta("rng_state"), state)
            self.assertEqual(len(resumed.load_pending()), 1)
            self.assertEqual(resumed.load_pending()[0]["mutation_seconds"], 0.123)
            case_dir = root / "case_00000"
            case_dir.mkdir()
            (case_dir / "payload.json").write_text("{}")
            row = {"case": 0, "energy": 0.5, "map_source": "sample.map"}
            counts = {"other": 1}
            resumed.complete(0, row, False, ("task",), {"status": "solved"},
                             (1.0, 2.0), True, True, 0.2, counts, 0, 0,
                             {}, 1, 0.0, 2)
            (root / "results.jsonl").write_text("stale row\n")
            resumed.sync_outputs()
            self.assertEqual([json.loads(line) for line in
                              (root / "results.jsonl").read_text().splitlines()], [row])
            self.assertFalse(case_dir.exists())
            self.assertEqual(resumed.completed_count(), 1)
            self.assertEqual(resumed.load_pending(), [])
            self.assertEqual(len(resumed.load_seed_pool()), 1)
            self.assertEqual(resumed.load_features(), [(1.0, 2.0)])
            self.assertEqual(resumed.meta("annoy_indexed_count"), 2)
            resumed.close()

    def test_failed_worker_submission_keeps_dispatched_case_pending(self):
        class FailingPool:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def submit(self, *_args):
                raise RuntimeError("pool unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_file = root / "small.map"
            map_file.write_text("type octile\nheight 8\nwidth 8\nmap\n" +
                                "........\n" * 8)
            args = SimpleNamespace(seed=1, resume=False, checkpoint=True,
                                   novelty_search="exact", workers=2,
                                   init_policy="uniform", case_retention="all",
                                   normal_sample_every=500, init_cases=1,
                                   time_limit=None, map_dir=None, map=map_file,
                                   size=8, agents=2, cases=2, mode="baseline",
                                   patch_size=2, solver="pibt", timeout=2,
                                   lacam_time=3, max_timestep=1000,
                                   threshold=0.25, agent_mutation_rate=0.5,
                                   guidance_rate=0.8, output=root / "output")
            with patch.object(run_pairs, "ProcessPoolExecutor", FailingPool):
                with self.assertRaisesRegex(RuntimeError, "pool unavailable"):
                    run_pairs.run(args)
            journal = RunJournal(args.output, json.loads(
                (args.output / "manifest.json").read_text()), True)
            self.assertEqual(journal.completed_count(), 0)
            self.assertEqual([item["case"] for item in journal.load_pending()], [0])
            self.assertEqual(journal.meta("next_case"), 1)
            journal.close()

    def test_completion_time_and_phase_fields_are_checkpointed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_file = root / "small.map"
            map_file.write_text("type octile\nheight 8\nwidth 8\nmap\n" +
                                "........\n" * 8)
            args = SimpleNamespace(seed=1, resume=False, checkpoint=True,
                                   novelty_search="exact", workers=1,
                                   init_policy="uniform", case_retention="all",
                                   normal_sample_every=500, init_cases=1,
                                   time_limit=None, map_dir=None, map=map_file,
                                   size=8, agents=2, cases=2, mode="baseline",
                                   patch_size=2, solver="pibt", timeout=2,
                                   lacam_time=3, max_timestep=1000,
                                   threshold=0.25, agent_mutation_rate=0.5,
                                   guidance_rate=0.8, output=root / "output")

            def fake_execute(_task, _solver, case_dir, _label, *_args):
                case_dir.mkdir(parents=True, exist_ok=True)
                time.sleep(0.001)
                return {"status": "unsolved", "reported_soc": None,
                        "_phase_seconds": {"solver": 0.0005,
                                           "result_analysis": 0.0001}}

            with patch.object(run_pairs, "execute", side_effect=fake_execute), \
                 redirect_stdout(io.StringIO()):
                run_pairs.run(args)
            rows = [json.loads(line) for line in
                    (args.output / "results.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertIsNone(rows[0]["fuzz_elapsed_seconds"])
            self.assertGreaterEqual(rows[1]["fuzz_elapsed_seconds"], 0)
            for row in rows:
                self.assertTrue(row["completed_at_utc"].endswith("Z"))
                self.assertTrue(row["timing_complete"])
                self.assertGreater(row["phase_seconds"]["solver"], 0)
                self.assertGreater(row["phase_seconds"]["metamorphic"], 0)
                self.assertGreater(row["phase_seconds"]["result_analysis"], 0)
                self.assertNotIn("_phase_seconds", row["results"]["original"])
            args.resume = True
            with patch.object(run_pairs, "execute", side_effect=AssertionError("reran case")):
                run_pairs.run(args)

    def test_cached_pending_keeps_original_worker_completion_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_file = root / "small.map"
            map_file.write_text("type octile\nheight 8\nwidth 8\nmap\n" +
                                "........\n" * 8)
            args = SimpleNamespace(seed=1, resume=False, checkpoint=True,
                                   novelty_search="exact", workers=1,
                                   init_policy="uniform", case_retention="all",
                                   normal_sample_every=500, init_cases=1,
                                   time_limit=None, map_dir=None, map=map_file,
                                   size=8, agents=2, cases=2, mode="baseline",
                                   patch_size=2, solver="pibt", timeout=2,
                                   lacam_time=3, max_timestep=1000,
                                   threshold=0.25, agent_mutation_rate=0.5,
                                   guidance_rate=0.8, output=root / "output")

            def fake_execute(_task, _solver, case_dir, _label, *_args):
                case_dir.mkdir(parents=True, exist_ok=True)
                return {"status": "unsolved", "reported_soc": None,
                        "_phase_seconds": {"solver": 0.0001,
                                           "result_analysis": 0.0001}}

            original_complete = RunJournal.complete

            def fail_before_commit(journal, case, *rest):
                if case == 1:
                    raise RuntimeError("synthetic interruption")
                return original_complete(journal, case, *rest)

            with patch.object(run_pairs, "execute", side_effect=fake_execute), \
                 patch.object(RunJournal, "complete", fail_before_commit), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                    run_pairs.run(args)
            cached = json.loads((args.output / "case_00001" / "payload.json").read_text())
            time.sleep(0.02)
            args.resume = True
            with patch.object(run_pairs, "execute", side_effect=AssertionError("reran case")), \
                 redirect_stdout(io.StringIO()):
                run_pairs.run(args)
            rows = [json.loads(line) for line in
                    (args.output / "results.jsonl").read_text().splitlines()]
            self.assertEqual(rows[1]["completed_at_utc"], cached["worker_completed_at_utc"])
            self.assertEqual(rows[1]["fuzz_elapsed_seconds"],
                             round(cached["worker_fuzz_elapsed_seconds"], 3))
            summary = json.loads((args.output / "summary.json").read_text())
            self.assertGreaterEqual(summary["fuzz_elapsed_seconds"],
                                    rows[1]["fuzz_elapsed_seconds"])

    def test_server_resume_verifies_finished_repeat_and_resumes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_dir = root / "maps"
            map_dir.mkdir()
            (map_dir / "sample.map").write_text("map\n")
            args = SimpleNamespace(solver="pibt", map_dir=map_dir,
                                   output_root=root, repeats=2, cases=3,
                                   time_limit=60, workers=2, init_cases=1,
                                   timeout=5, seed=1)
            plan = run_server.build_plan(args)
            run_root = Path(plan["output_root"])
            run_root.mkdir()
            (run_root / "server_manifest.json").write_text(json.dumps(plan))
            first = Path(plan["jobs"][0]["output"])
            first.mkdir(parents=True)
            (first / "checkpoint.sqlite3").touch()
            (first / "summary.json").write_text(json.dumps({"stop_reason": "case_limit"}))
            second = Path(plan["jobs"][1]["output"])
            second.mkdir(parents=True)
            (second / "checkpoint.sqlite3").touch()
            argv = ["run_server.py", "--resume-run", str(run_root), "--execute"]
            with patch.object(sys, "argv", argv), \
                 patch.object(run_server.importlib, "import_module",
                              return_value=SimpleNamespace(AnnoyIndex=object)), \
                 patch.object(run_server.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0)) as call, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(run_server.main(), 0)
            self.assertEqual(call.call_count, 2)
            commands = [entry.args[0] for entry in call.call_args_list]
            self.assertTrue(all("--resume" in command for command in commands))
            self.assertEqual([command[command.index("--output") + 1]
                              for command in commands], [str(first), str(second)])

    def test_rq3_entry_uses_separate_single_worker_profile(self):
        argv = ["run_rq3.py", "--solver", "pibt", "--execute"]
        with patch.object(sys, "argv", argv), \
             patch.object(run_rq3.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0)) as call:
            self.assertEqual(run_rq3.main(), 0)
        command = call.call_args.args[0]
        self.assertEqual(command[command.index("--profile") + 1], "rq3_efficiency")
        self.assertEqual(command[command.index("--workers") + 1], "1")
        self.assertEqual(command[command.index("--repeats") + 1], "1")
        self.assertEqual(command[command.index("--time-limit") + 1], "3600")
        self.assertIn("--execute", command)
        self.assertIn("reproduce/output/rq3", command[command.index("--output-root") + 1])


if __name__ == "__main__":
    unittest.main()
