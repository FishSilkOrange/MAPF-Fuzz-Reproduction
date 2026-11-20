"""Validate completed RQ1/RQ3 run outputs before archiving or plotting.

This is an offline, read-only check. It accepts a copied run directory too:
paths stored in the server plan may still point to the original server.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path


FINDINGS = ("completeness_failure", "cost_gap", "all_unsolved",
            "invalid_or_error", "other")
RQ3_CATEGORIES = ("mutation", "metamorphic", "execution", "result_analysis")
PROFILES = {"server_main_experiment": ("rq1", 20),
            "rq3_efficiency": ("rq3", 4)}


@dataclass
class Report:
    jobs_checked: int = 0
    cases_checked: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")


def read_json(path: Path, report: Report) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (OSError, ValueError) as exc:
        report.error(str(path), f"cannot read JSON: {exc}")
        return None


def nonnegative(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def validate_checkpoint(path: Path, expected_count: object, report: Report) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        report.error(str(path), "missing or empty checkpoint.sqlite3")
        return
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro",
                                     uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                report.error(str(path), "SQLite quick_check failed")
            result_count = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            pending_count = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            if result_count != expected_count:
                report.error(str(path), "checkpoint result count disagrees with summary")
            if pending_count:
                report.error(str(path), f"checkpoint has {pending_count} pending cases")
    except sqlite3.DatabaseError as exc:
        report.error(str(path), f"cannot validate SQLite checkpoint: {exc}")


def validate_artifacts(job_dir: Path, case: int, row: dict, solver: str,
                       report: Report) -> None:
    case_dir = job_dir / f"case_{case:05d}"
    finding = row.get("finding")
    if not case_dir.is_dir():
        message = f"selected case {case} has no retained directory"
        if finding == "invalid_or_error":
            report.warn(str(job_dir), message + " (execution may have failed before writing)")
        else:
            report.error(str(job_dir), message)
        return
    results = row.get("results")
    if not isinstance(results, dict) or "original" not in results:
        report.error(str(case_dir), "no original result in JSONL")
        return
    suffix = ".scen" if solver == "lacam3" else ".txt"
    for relation, result in results.items():
        if not isinstance(result, dict):
            report.error(str(case_dir), f"{relation}: invalid result record")
            continue
        relation_dir = case_dir / relation
        if result.get("status") == "execution_error" and not relation_dir.is_dir():
            report.warn(str(case_dir), f"{relation}: execution failed before files were written")
            continue
        for name in ("input.map", f"input{suffix}"):
            path = relation_dir / name
            if not path.is_file() or path.stat().st_size == 0:
                report.error(str(relation_dir), f"missing or empty {name}")
        if result.get("status") == "solved":
            path = relation_dir / "result.txt"
            if not path.is_file() or path.stat().st_size == 0:
                report.error(str(relation_dir), "solved result lacks nonempty result.txt")


def validate_job(job_dir: Path, job: dict, profile: str, report: Report,
                 require_post_init: bool, plan: dict) -> None:
    where = str(job_dir)
    manifest = read_json(job_dir / "manifest.json", report)
    summary = read_json(job_dir / "summary.json", report)
    if manifest is None or summary is None:
        return
    report.jobs_checked += 1
    for key in ("solver", "seed", "cases", "time_limit"):
        if manifest.get(key) != job.get(key):
            report.error(where, f"manifest.{key} disagrees with server job")
    for key in ("size", "agents", "workers", "novelty_search"):
        if manifest.get(key) != plan.get(key):
            report.error(where, f"manifest.{key} disagrees with server plan")
    if (not isinstance(manifest.get("init_cases"), int)
            or not isinstance(job.get("maximum_initial_cases"), int)
            or manifest["init_cases"] > job["maximum_initial_cases"]):
        report.error(where, "manifest.init_cases exceeds planned initial pool")
    if manifest.get("mode") != "mutate" or manifest.get("checkpoint") is not True:
        report.error(where, "server job must use mutation and checkpoints")
    if profile == "rq3_efficiency" and manifest.get("workers") != 1:
        report.error(where, "RQ3 needs one worker for comparable timing")
    if summary.get("manifest") != manifest:
        report.error(where, "summary.manifest disagrees with manifest.json")
    if summary.get("requested_cases") != manifest.get("cases"):
        report.error(where, "summary.requested_cases disagrees with manifest.cases")
    if summary.get("stop_reason") not in ("time_limit", "case_limit"):
        report.error(where, f"unfinished stop_reason={summary.get('stop_reason')!r}")
    elif summary["stop_reason"] == "case_limit":
        if summary.get("completed_cases") != manifest.get("cases"):
            report.error(where, "case_limit without all requested cases")
    elif (not nonnegative(summary.get("fuzz_elapsed_seconds"))
          or summary["fuzz_elapsed_seconds"] + 0.01 < manifest.get("time_limit", 0)):
        report.error(where, "time_limit before the planned fuzzing duration")
    for key in ("processing_error", "dispatch_error"):
        if summary.get(key):
            report.error(where, f"{key}={summary[key]!r}")
    validate_checkpoint(job_dir / "checkpoint.sqlite3",
                        summary.get("completed_cases"), report)

    init_cases = manifest.get("init_cases")
    if not isinstance(init_cases, int) or init_cases < 1:
        report.error(where, "manifest.init_cases must be positive")
        return
    seen: set[int] = set()
    counts: Counter[str] = Counter()
    cf_count = of_count = post_init = parent_mutations = retained = 0
    rq3_counted = rq3_incomplete = 0
    rq3_seconds = Counter({name: 0.0 for name in RQ3_CATEGORIES})
    path = job_dir / "results.jsonl"
    try:
        stream = path.open(encoding="utf-8")
    except OSError as exc:
        report.error(str(path), f"cannot read results: {exc}")
        return
    with stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected JSON object")
            except ValueError as exc:
                report.error(str(path), f"line {line_number}: {exc}")
                continue
            case = row.get("case")
            if not isinstance(case, int) or case < 0:
                report.error(str(path), f"line {line_number}: invalid case id {case!r}")
                continue
            if case in seen:
                report.error(str(path), f"duplicate case id {case}")
            seen.add(case)
            finding = row.get("finding")
            if finding not in FINDINGS:
                report.error(str(path), f"case {case}: unknown finding {finding!r}")
            else:
                counts[finding] += 1
            cf_count += row.get("cf") is True
            of_count += row.get("of") is True
            if not isinstance(row.get("results"), dict) or "original" not in row["results"]:
                report.error(str(path), f"case {case}: missing original result")
            elif (isinstance(row["results"]["original"], dict)
                  and row["results"]["original"].get("status") == "solved"
                  and set(row["results"]) != {"original", *manifest.get("relations", [])}):
                report.error(str(path), f"case {case}: solved original lacks seven MR results")
            if not row.get("completed_at_utc"):
                report.error(str(path), f"case {case}: missing completion timestamp")
            if case >= init_cases:
                post_init += 1
                parent_mutations += row.get("parent_case") is not None
                if not nonnegative(row.get("fuzz_elapsed_seconds")):
                    report.error(str(path), f"case {case}: missing fuzz elapsed time")
                if profile == "rq3_efficiency":
                    phases = row.get("phase_seconds")
                    if (row.get("timing_complete") is not True
                            or not isinstance(phases, dict)
                            or not all(nonnegative(phases.get(name))
                                       for name in RQ3_CATEGORIES)):
                        rq3_incomplete += 1
                        report.error(str(path), f"case {case}: incomplete RQ3 timing")
                    else:
                        rq3_counted += 1
                        for name in RQ3_CATEGORIES:
                            rq3_seconds[name] += phases[name]
            if row.get("case_files_selected_for_retention") is True:
                retained += 1
                validate_artifacts(job_dir, case, row, str(job.get("solver")), report)
            elif row.get("case_files_selected_for_retention") is not False:
                report.error(str(path), f"case {case}: missing retention decision")
    report.cases_checked += len(seen)
    if seen != set(range(len(seen))):
        report.error(where, "case ids are not contiguous from zero")
    if summary.get("completed_cases") != len(seen):
        report.error(where, "summary.completed_cases differs from unique JSONL case count")
    summary_counts = summary.get("counts")
    if not isinstance(summary_counts, dict) or set(summary_counts) != set(FINDINGS):
        report.error(where, "summary.counts lacks the five mutually exclusive findings")
    elif any(summary_counts[name] != counts[name] for name in FINDINGS):
        report.error(where, "summary.counts disagrees with JSONL findings")
    if isinstance(summary_counts, dict) and sum(
            value for value in summary_counts.values() if isinstance(value, int)) != len(seen):
        report.error(where, "summary.counts does not sum to completed_cases")
    if summary.get("cf_count") != cf_count or summary.get("of_count") != of_count:
        report.error(where, "summary CF/OF counts disagree with JSONL flags")
    if require_post_init and post_init == 0:
        report.error(where, "no completed post-initialization case")
    if require_post_init and profile == "server_main_experiment" and parent_mutations == 0:
        report.error(where, "no post-initialization case selected a parent seed")
    if retained == 0:
        report.warn(where, "no retained case; case-level figures cannot be made from this job")

    if profile == "rq3_efficiency":
        timing = summary.get("rq3_timing")
        if not isinstance(timing, dict):
            report.error(where, "missing rq3_timing summary")
            return
        if timing.get("post_initialization_cases") != rq3_counted:
            report.error(where, "RQ3 counted case total disagrees with JSONL")
        if timing.get("excluded_incomplete_cases") != rq3_incomplete:
            report.error(where, "RQ3 incomplete case total disagrees with JSONL")
        seconds = timing.get("seconds")
        percents = timing.get("category_percent")
        if not isinstance(seconds, dict) or not isinstance(percents, dict):
            report.error(where, "RQ3 seconds/category_percent missing")
            return
        denominator = sum(rq3_seconds.values())
        if denominator <= 0 and require_post_init:
            report.error(where, "RQ3 four-category denominator is zero")
        if not nonnegative(timing.get("categorized_seconds")) or not math.isclose(
                timing["categorized_seconds"], denominator, abs_tol=0.01):
            report.error(where, "RQ3 categorized_seconds disagrees with JSONL")
        for name in RQ3_CATEGORIES:
            if not nonnegative(seconds.get(name)) or not math.isclose(
                    seconds[name], rq3_seconds[name], abs_tol=0.01):
                report.error(where, f"RQ3 seconds.{name} disagrees with JSONL")
            percent = percents.get(name)
            expected = 100 * rq3_seconds[name] / denominator if denominator else None
            if expected is not None and (not nonnegative(percent) or not math.isclose(
                    percent, expected, abs_tol=0.02)):
                report.error(where, f"RQ3 category_percent.{name} disagrees with JSONL")
        if denominator > 0 and not math.isclose(
                sum(value for name in RQ3_CATEGORIES
                    if nonnegative(value := percents.get(name))), 100, abs_tol=0.02):
            report.error(where, "RQ3 four category percentages do not sum to 100")
        if not nonnegative(timing.get("wall_seconds")):
            report.error(where, "RQ3 wall_seconds missing")


def validate_run(root: Path, expected_jobs: int | None = None,
                 require_post_init: bool = True) -> Report:
    report = Report()
    plan = read_json(root / "server_manifest.json", report)
    if plan is None:
        return report
    profile = plan.get("profile")
    if profile not in PROFILES:
        report.error(str(root), f"unsupported profile {profile!r}")
        return report
    jobs = plan.get("jobs")
    if not isinstance(jobs, list):
        report.error(str(root), "server_manifest.jobs is not a list")
        return report
    target = expected_jobs if expected_jobs is not None else PROFILES[profile][1]
    if len(jobs) != target:
        report.error(str(root), f"planned {len(jobs)} jobs; expected {target}")
    keys: set[tuple[str, int]] = set()
    for job in jobs:
        if not isinstance(job, dict):
            report.error(str(root), "invalid job in server_manifest")
            continue
        solver, repeat = job.get("solver"), job.get("repeat")
        if (not isinstance(solver, str) or not solver.isidentifier()
                or not isinstance(repeat, int) or repeat < 1):
            report.error(str(root), f"invalid solver/repeat in job: {job!r}")
            continue
        key = (solver, repeat)
        if key in keys:
            report.error(str(root), f"duplicate planned job {solver} repeat {repeat}")
            continue
        keys.add(key)
        job_dir = root / solver / f"repeat_{repeat:02d}"
        validate_job(job_dir, job, profile, report, require_post_init, plan)
    if report.jobs_checked != len(jobs):
        report.error(str(root), f"read {report.jobs_checked} of {len(jobs)} planned jobs")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path,
                        help="timestamped directory containing server_manifest.json")
    parser.add_argument("--expected-jobs", type=int,
                        help="override default: RQ1=20, RQ3=4 (use 4/1 for preflight)")
    parser.add_argument("--allow-init-only", action="store_true",
                        help="skip post-initialization gate for exploratory runs")
    args = parser.parse_args()
    if args.expected_jobs is not None and args.expected_jobs < 1:
        parser.error("--expected-jobs must be positive")
    report = validate_run(args.run_root, args.expected_jobs, not args.allow_init_only)
    for warning in report.warnings:
        print(f"WARN {warning}")
    for error in report.errors:
        print(f"FAIL {error}")
    print(f"{'PASS' if not report.errors else 'FAIL'}: "
          f"{report.jobs_checked} jobs, {report.cases_checked} unique cases, "
          f"{len(report.errors)} errors, {len(report.warnings)} warnings")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
