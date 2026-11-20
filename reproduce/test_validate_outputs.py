"""Acceptance checks for offline RQ1/RQ3 results; no solver needed."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reproduce.validate_outputs import validate_run


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def make_run(root: Path, profile: str = "server_main_experiment") -> Path:
    job_dir = root / "pibt" / "repeat_01"
    job_dir.mkdir(parents=True)
    job = {"solver": "pibt", "repeat": 1, "seed": 1, "cases": 2,
           "maximum_initial_cases": 1,
           "time_limit": 120.0, "output": str(job_dir)}
    write_json(root / "server_manifest.json", {
        "profile": profile, "jobs": [job], "size": 128, "agents": 100,
        "workers": 1 if profile == "rq3_efficiency" else 6,
        "novelty_search": "annoy"})
    relations = ("swap_start_goal", "order", "left_right", "up_down",
                 "rotate_180", "add_probe", "add_obstacle")
    manifest = {"solver": "pibt", "seed": 1, "cases": 2, "time_limit": 120.0,
                "init_cases": 1, "size": 128, "agents": 100,
                "workers": 1 if profile == "rq3_efficiency" else 6,
                "novelty_search": "annoy", "mode": "mutate", "checkpoint": True,
                "relations": list(relations)}
    write_json(job_dir / "manifest.json", manifest)
    rows = []
    for case in (0, 1):
        rows.append({"case": case, "parent_case": None if case == 0 else 0,
                     "finding": "other", "cf": False, "of": False,
                     "case_files_selected_for_retention": case == 0,
                     "results": {name: {"status": "solved"}
                                 for name in ("original", *relations)},
                     "completed_at_utc": "2026-01-01T00:00:00Z",
                     "fuzz_elapsed_seconds": None if case == 0 else 1.2,
                     "timing_complete": True,
                     "phase_seconds": {"mutation": 1, "metamorphic": 2,
                                       "execution": 6, "solver": 5,
                                       "result_parse": 0.5,
                                       "result_analysis": 1}})
    (job_dir / "results.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with sqlite3.connect(job_dir / "checkpoint.sqlite3") as conn:
        conn.execute("CREATE TABLE results (case_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE pending (case_id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO results(case_id) VALUES (?)", ((0,), (1,)))
    for relation in ("original", *relations):
        artifact_dir = job_dir / "case_00000" / relation
        artifact_dir.mkdir(parents=True)
        for name in ("input.map", "input.txt", "result.txt"):
            (artifact_dir / name).write_text("fixture\n")
    summary = {"manifest": manifest, "requested_cases": 2,
               "completed_cases": 2, "stop_reason": "case_limit",
               "processing_error": None, "dispatch_error": None,
               "counts": {"completeness_failure": 0, "cost_gap": 0,
                          "all_unsolved": 0, "invalid_or_error": 0, "other": 2},
               "cf_count": 0, "of_count": 0}
    if profile == "rq3_efficiency":
        summary["rq3_timing"] = {
            "post_initialization_cases": 1, "excluded_incomplete_cases": 0,
            "seconds": {"mutation": 1, "metamorphic": 2,
                        "execution": 6, "result_analysis": 1},
            "categorized_seconds": 10,
            "category_percent": {"mutation": 10, "metamorphic": 20,
                                 "execution": 60, "result_analysis": 10},
            "wall_seconds": 12}
    write_json(job_dir / "summary.json", summary)
    return job_dir


class ValidateOutputsTests(unittest.TestCase):
    def test_complete_rq1_and_rq3_preflight_pass(self):
        for profile in ("server_main_experiment", "rq3_efficiency"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                make_run(root, profile)
                report = validate_run(root, expected_jobs=1)
                self.assertEqual(report.errors, [])
                self.assertEqual((report.jobs_checked, report.cases_checked), (1, 2))

    def test_duplicate_case_and_missing_retained_artifact_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = make_run(root)
            result_path = job_dir / "results.jsonl"
            first_line = result_path.read_text().splitlines()[0]
            with result_path.open("a") as stream:
                stream.write(first_line + "\n")
            (job_dir / "case_00000" / "original" / "input.map").unlink()
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("duplicate case id" in error for error in report.errors))
            self.assertTrue(any("missing or empty input.map" in error for error in report.errors))

    def test_empty_retained_artifact_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = make_run(root)
            (job_dir / "case_00000" / "original" / "result.txt").write_text("")
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("lacks nonempty result.txt" in error
                                for error in report.errors))

    def test_empty_or_inconsistent_checkpoint_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = make_run(root)
            checkpoint = job_dir / "checkpoint.sqlite3"
            with sqlite3.connect(checkpoint) as conn:
                conn.execute("DELETE FROM results WHERE case_id=1")
                conn.execute("INSERT INTO pending(case_id) VALUES (1)")
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("checkpoint result count disagrees" in error
                                for error in report.errors))
            self.assertTrue(any("checkpoint has 1 pending" in error
                                for error in report.errors))
            checkpoint.write_bytes(b"")
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("missing or empty checkpoint" in error
                                for error in report.errors))

    def test_rq3_missing_timing_and_percent_error_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = make_run(root, "rq3_efficiency")
            summary_path = job_dir / "summary.json"
            summary = json.loads(summary_path.read_text())
            summary["rq3_timing"]["category_percent"]["execution"] = 70
            write_json(summary_path, summary)
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("percentages do not sum" in error for error in report.errors))

            result_path = job_dir / "results.jsonl"
            rows = [json.loads(line) for line in result_path.read_text().splitlines()]
            rows[1]["timing_complete"] = False
            result_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("incomplete RQ3 timing" in error for error in report.errors))

    def test_stop_reason_must_match_completed_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_dir = make_run(root)
            summary_path = job_dir / "summary.json"
            summary = json.loads(summary_path.read_text())
            summary["completed_cases"] = 1
            write_json(summary_path, summary)
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("case_limit without all requested cases" in error
                                for error in report.errors))

            summary["stop_reason"] = "time_limit"
            summary["fuzz_elapsed_seconds"] = 20.0
            write_json(summary_path, summary)
            report = validate_run(root, expected_jobs=1)
            self.assertTrue(any("time_limit before the planned fuzzing duration" in error
                                for error in report.errors))


if __name__ == "__main__":
    unittest.main()
