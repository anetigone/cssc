# Benchmark scripts

The benchmark code is split into three layers:

1. `benchmark_harness.py` is the suite-neutral API. New suites configure it;
   they do not modify another phase's module globals.
2. `phase8_benchmark_*.py` are the historical Phase 8 entry points and the
   compatibility backend for trace/replay formats created in that phase.
3. `phase10_benchmark_*.py` contain Phase 10 policy, validation gates and CLI
   configuration. Phase 10 data lives only under
   `tests/fixtures/phase10_benchmark` and `.runs/phase10`.

`phase8_benchmark_replay.py` remains named for compatibility with existing
tests and imports. New code should import replay support through
`scripts.benchmark_harness`.

Historical `.runs/phase8/stage1-canary` data is pipeline-smoke evidence and is
never an input to Phase 10 reports or calibration.

`PHASE10_ARM_FEATURES` records the intended experiment design; it is not an
execution mechanism.  An arm is runnable only if each declared dimension is
wired into the controller.  Today controlled replay can execute `C0` (legacy
branch frontier) and `C1` (the bundled action-cost-aware runtime).  It rejects
`C2`/`C3` because empirical cost and remaining-budget admission are not
independent runtime switches, and rejects `C4` because replay makes no model
calls on which cheap/strong routing could act.  This prevents labels in
provenance from being mistaken for real ablations.

On the live track, `A5` does enable the Phase 9.4 cheap/strong router and `A6`
does not.  That routing distinction does not make the still-unwired cost-source
or admission-policy dimensions executable.
