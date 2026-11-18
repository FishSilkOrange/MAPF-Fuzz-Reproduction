"""Run the paper's single-worker, one-hour-per-solver RQ3 timing profile."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "reproduce/run_server.py"
OUTPUT = ROOT / "reproduce/output/rq3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver", choices=("all", "pibt", "hca", "pibt_plus", "lacam3"),
                        default="all")
    parser.add_argument("--map-dir", type=Path, default=ROOT / "data/maps")
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--time-limit", type=float, default=3600,
                        help="seconds after initial seeds; 3600 in the paper")
    parser.add_argument("--cases", type=int, default=1_000_000,
                        help="safety ceiling so the one-hour limit normally stops the run")
    parser.add_argument("--resume-run", type=Path,
                        help="continue an existing RQ3 timestamp directory")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.time_limit <= 0 or args.cases < 1:
        parser.error("time-limit and cases must be positive")
    command = [sys.executable, str(SERVER),
               "--profile", "rq3_efficiency",
               "--solver", args.solver,
               "--map-dir", str(args.map_dir),
               "--output-root", str(args.output_root),
               "--workers", "1", "--repeats", "1",
               "--time-limit", str(args.time_limit),
               "--cases", str(args.cases),
               "--seed", str(args.seed)]
    if args.resume_run is not None:
        command += ["--resume-run", str(args.resume_run)]
    if args.execute:
        command.append("--execute")
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
