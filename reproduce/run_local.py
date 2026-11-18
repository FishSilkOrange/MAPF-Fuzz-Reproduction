"""Run the local 128x128 / 50-agent smoke profile in an isolated output tree."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "reproduce/run_pairs.py"
MAPS = ROOT / "data/maps"
SOLVERS = ("pibt", "hca", "pibt_plus", "lacam3")
LOCAL_OUTPUT = ROOT / "reproduce/output/local"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver", choices=("all", *SOLVERS), default="all")
    parser.add_argument("--map", type=Path, default=MAPS / "Berlin_0_256.map")
    parser.add_argument("--cases", type=int, default=3,
                        help="number of local smoke-test task groups per solver")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--mode", choices=("baseline", "mutate"), default="mutate")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--lacam-time", type=float, default=3)
    args = parser.parse_args()
    if args.cases < 1 or args.timeout <= 0 or args.lacam_time < 1:
        parser.error("cases and timeout must be positive; lacam-time must be at least 1")
    if not args.map.is_file():
        parser.error(f"map does not exist: {args.map}")
    if not CORE.is_file():
        parser.error(f"shared experiment runner does not exist: {CORE}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_root = LOCAL_OUTPUT / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    solvers = SOLVERS if args.solver == "all" else (args.solver,)
    local_manifest = {
        "profile": "local_smoke",
        "run_id_utc": run_id,
        "map": str(args.map.resolve()),
        "size": 128,
        "agents": 50,
        "workers": 1,
        "cases_per_solver": args.cases,
        "solver_list": list(solvers),
        "seed": args.seed,
        "mode": args.mode,
        "patch_size": 32,
        "threshold": 0.25,
        "timeout_seconds_per_call": args.timeout,
        "lacam_optimization_seconds": args.lacam_time,
        "max_timestep": 1000,
        "novelty_search": "exact",
        "output_root": str(run_root),
    }
    (run_root / "local_manifest.json").write_text(
        json.dumps(local_manifest, indent=2) + "\n")

    for solver in solvers:
        output = run_root / solver
        command = [
            sys.executable, str(CORE),
            "--solver", solver,
            "--map", str(args.map.resolve()),
            "--size", "128",
            "--agents", "50",
            "--cases", str(args.cases),
            "--seed", str(args.seed),
            "--mode", args.mode,
            "--patch-size", "32",
            "--threshold", "0.25",
            "--timeout", str(args.timeout),
            "--lacam-time", str(args.lacam_time),
            "--max-timestep", "1000",
            "--novelty-search", "exact",
            "--output", str(output),
        ]
        print(f"Local profile: solver={solver}; output={output}", flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0:
            print(f"Stopped after {solver} returned {completed.returncode}; "
                  f"partial files are kept in {run_root}", file=sys.stderr)
            return completed.returncode
    print(f"Local smoke outputs: {run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
