"""Plan or launch the 128x128 / 100-agent server experiment profile."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "reproduce/run_pairs.py"
MAPS = ROOT / "data/maps"
SERVER_OUTPUT = ROOT / "reproduce/output/server"
SOLVERS = ("pibt", "hca", "pibt_plus", "lacam3")


def build_plan(args: argparse.Namespace) -> dict:
    solvers = SOLVERS if args.solver == "all" else (args.solver,)
    map_files = sorted(args.map_dir.glob("*.map"))
    maximum_initial_cases = min(args.init_cases, 3 * len(map_files))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_root = args.output_root.resolve() / run_id
    jobs = []
    for solver in solvers:
        for repeat in range(1, args.repeats + 1):
            cases = args.cases if args.cases is not None else (25000 if solver == "lacam3" else 20000)
            duration = (args.time_limit if args.time_limit is not None
                        else (21600 if solver == "lacam3" else 3600))
            output = run_root / solver / f"repeat_{repeat:02d}"
            seed = args.seed + (SOLVERS.index(solver) * 1000) + repeat - 1
            jobs.append({"solver": solver, "repeat": repeat, "seed": seed,
                         "cases": cases, "time_limit": duration,
                         "maximum_initial_cases": min(cases, maximum_initial_cases),
                         "output": str(output)})
    return {"profile": getattr(args, "profile", "server_main_experiment"),
            "run_id_utc": run_id,
            "output_root": str(run_root), "map_dir": str(args.map_dir.resolve()),
            "maps": [str(path.resolve()) for path in map_files],
            "initial_map_sampling": "up_to_three_per_map_seed_42",
            "case_file_retention": "failures_and_every_500th_normal_case",
            "novelty_search": "annoy",
            "checkpoint": "sqlite_after_each_dispatch_and_result",
            "size": 128, "agents": 100, "workers": args.workers,
            "init_cases": args.init_cases, "repeats": args.repeats,
            "timeout_seconds_per_call": args.timeout,
            "lacam_optimization_seconds": 3, "threshold": 0.25,
            "max_timestep": 30000, "jobs": jobs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("server_main_experiment", "rq3_efficiency"),
                        default="server_main_experiment")
    parser.add_argument("--solver", choices=("all", *SOLVERS), default="all")
    parser.add_argument("--map-dir", type=Path, default=MAPS)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--init-cases", type=int, default=100)
    parser.add_argument("--cases", type=int,
                        help="override paper case limit for a server shakedown")
    parser.add_argument("--time-limit", type=float,
                        help="override paper time limit in seconds for a server shakedown")
    parser.add_argument("--timeout", type=float, default=40,
                        help="wall-clock limit per solver call, from author settings")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output-root", type=Path, default=SERVER_OUTPUT)
    parser.add_argument("--resume-run", type=Path,
                        help="continue the exact server_manifest.json plan in this run directory")
    parser.add_argument("--execute", action="store_true",
                        help="launch the planned jobs; omitted means print plan only")
    args = parser.parse_args()
    if (args.repeats < 1 or args.workers < 1 or args.init_cases < 1
            or args.timeout <= 0 or (args.cases is not None and args.cases < 1)
            or (args.time_limit is not None and args.time_limit <= 0)):
        parser.error("repeats, workers, init-cases, cases, time-limit and timeout must be positive")
    if args.resume_run is None and (not args.map_dir.is_dir()
                                    or not list(args.map_dir.glob("*.map"))):
        parser.error(f"map directory has no .map files: {args.map_dir}")
    if not CORE.is_file():
        parser.error(f"shared experiment runner does not exist: {CORE}")
    if args.resume_run is None:
        plan = build_plan(args)
    else:
        run_manifest = args.resume_run.resolve() / "server_manifest.json"
        if not run_manifest.is_file():
            parser.error(f"server manifest does not exist: {run_manifest}")
        plan = json.loads(run_manifest.read_text())
        if (plan.get("profile") != args.profile
                or Path(plan.get("output_root", "")).resolve() != args.resume_run.resolve()):
            parser.error("resume-run does not match the saved server plan")
        if not Path(plan["map_dir"]).is_dir():
            parser.error(f"saved map directory does not exist: {plan['map_dir']}")
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    for module_name, required_attribute in (("annoy", "AnnoyIndex"),
                                            ("numpy", "ndarray"),
                                            ("noise", "pnoise2")):
        try:
            getattr(importlib.import_module(module_name), required_attribute)
        except (ImportError, OSError, AttributeError):
            parser.error(f"server mode needs {module_name}: install "
                         "reproduce/requirements-server.txt in the active Python environment")
    run_root = Path(plan["output_root"])
    if args.resume_run is None:
        run_root.mkdir(parents=True, exist_ok=False)
        (run_root / "server_manifest.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    for job in plan["jobs"]:
        output = Path(job["output"])
        checkpoint = output / "checkpoint.sqlite3"
        if args.resume_run is not None and output.exists() and not checkpoint.is_file():
            if not output.is_dir() or any(
                    path.name != "checkpoint.lock" for path in output.iterdir()):
                parser.error(f"cannot resume job with data but no checkpoint: {output}")
        command = [sys.executable, str(CORE),
                   "--solver", job["solver"],
                   "--map-dir", plan["map_dir"],
                   "--size", "128", "--agents", "100",
                   "--cases", str(job["cases"]),
                   "--workers", str(plan["workers"]),
                   "--init-cases", str(plan["init_cases"]),
                   "--init-policy", "per_map_three",
                   "--case-retention", "failures_samples",
                   "--normal-sample-every", "500",
                   "--novelty-search", "annoy",
                   "--time-limit", str(job["time_limit"]),
                   "--seed", str(job["seed"]),
                   "--mode", "mutate", "--patch-size", "32",
                   "--threshold", "0.25", "--timeout", str(plan["timeout_seconds_per_call"]),
                   "--lacam-time", "3", "--max-timestep", "30000",
                   "--output", job["output"]]
        command.append("--resume" if checkpoint.is_file() else "--checkpoint")
        print(f"Server job: {job['solver']} repeat {job['repeat']}; output={job['output']}",
              flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0:
            print(f"Stopped after exit code {completed.returncode}; files kept in {run_root}",
                  file=sys.stderr)
            return completed.returncode
    print(f"Server outputs: {run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
