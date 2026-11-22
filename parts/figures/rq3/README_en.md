# RQ3 main-experiment figures (English)

Source: `reproduce/output/rq3/20261120T040544.559020Z`, four solver `summary.json` files and the run manifests. All four jobs used 128×128 maps, 100 agents, Annoy, and one worker; each reached the one-hour time limit after initialization. There were no incomplete post-initialization timings. These figures use the main experiment, not the preflight data.

- `rq3_phase_share_en.svg/png` — Main RQ3 result slide. It answers which of the four phases consumes each solver's categorized time. The denominator is the sum of mutation, MR generation, execution, and result analysis; initial seeds are excluded. Put this directly after the RQ3 experimental setup.
- `rq3_absolute_time_en.svg/png` — Follow-up or backup slide. It shows the four phases in actual seconds, plus replay artifact I/O and unattributed wall time. The residual includes scheduling and checkpointing; it is **not** an exact checkpoint timer.
- `rq3_execution_detail_en.svg/png` — Optional diagnostic slide after the main chart when explaining why LaCAM* looks different. Solver process and result parsing are **inside** execution and are not added to the four-phase totals. Parsing includes trajectory validation.

Suggested narration: PIBT, HCA, and PIBT+ spend about half of categorized time on result analysis. LaCAM* spends 92.8% on execution, 97.5% of which is in the solver process. The post-initialization task counts are 1,340, 1,150, 1,304, and 133 respectively. Each solver has one run, so these charts do not establish variance. Comparison with paper Fig. 6 is approximate because the implementation validates trajectories and uses checkpoints.
