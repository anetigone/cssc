# Phase 10 controlled/live benchmark

Suite `phase10-canary-v1` is the six-task preflight gate. Lean files are model-facing
scaffolds and intentionally contain no benchmark commentary or proof hints. All
evaluation semantics live in `manifest.jsonl` and `scenarios/`.

The CLI entry points are exclusively `scripts/phase10_benchmark_*.py`. Shared
execution lives behind `scripts/benchmark_harness.py`; callers must not invoke
or mutate Phase 8 modules to configure this suite.

Controlled costs are simulations, never billed usage. Live runs require frozen model,
provider, history, pricing and budget provenance. `.runs/phase8/stage1-canary` is
historical `pipeline-smoke-v1` evidence only and must never be aggregated here.
