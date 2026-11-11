# MAPF-Fuzz Reproduction

Reproduction workspace for the ICSE 2026 paper *Metamorphic Fuzzing for Multi-Agent Path Finding Algorithms*.

The repository contains:

- the paper and its released MAPF-Fuzz artifact under `code/`;
- reproducible Python 3.11 environment metadata in `environment.yml`;
- pinned official solver revisions in `solver-lock.json`;
- PIBT2 and LaCAM3 as Git submodules under `code/third_party/`.

## Environment

Create the environment with Miniforge or Conda.  Install the PyPI
dependencies as a separate transaction: it prevents Conda/Pip mixed solves
from obscuring a failure and makes setup resumable.

```bash
conda env create --prefix .conda/envs/mapf-fuzz --file environment.yml
conda activate "$PWD/.conda/envs/mapf-fuzz"
python -m pip install -r code/requirements.txt
```

## Solver build

```bash
git submodule update --init code/third_party/pibt2 code/third_party/lacam3
git -C code/third_party/pibt2 submodule update --init \
  third_party/googletest third_party/grid-pathfinding
git -C code/third_party/lacam3 submodule update --init --recursive
# GCC 15 requires the following compatibility include for the pinned PIBT2
# grid-pathfinding dependency (no solver source is changed).
cmake -S code/third_party/pibt2 -B code/third_party/pibt2/build \
  -DCMAKE_CXX_FLAGS="-include cstring"
cmake --build code/third_party/pibt2/build --parallel
cmake -S code/third_party/lacam3 -B code/third_party/lacam3/build
cmake --build code/third_party/lacam3/build --parallel
```

The expected executables are:

- `code/third_party/pibt2/build/mapf`
- `code/third_party/lacam3/build/main`

`pibt2/third_party/openFrameworks` is deliberately not initialized: it is
only needed by PIBT2's graphical visualizer, not by the solvers or tests.

## Verification

```bash
(cd code/third_party/pibt2/build && ./test --gtest_color=no)
ctest --test-dir code/third_party/lacam3/build --output-on-failure
```

The MAPF-Fuzz Python adapter has also been smoke-tested against all four
configured algorithms: `pibt`, `hca`, `pibt_plus`, and `lacam3`.

The solver revisions are provisional artifact-faithful candidates and must be calibrated against the bundled defect corpus before full experiments.

