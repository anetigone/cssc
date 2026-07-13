# Benchmark scripts

`benchmark_harness.py` is the suite-neutral entry point. New benchmark adapters
and suites should configure this layer rather than mutate historical module
globals.

## Historical compatibility entry points

- `phase8_benchmark_*.py` retain the original trace/replay backend names.
- `phase10_benchmark_*.py` retain the internal controlled/live canary names.
- Their names, trace layouts and serialized arm identifiers are compatibility
  surfaces; they do not define the current development phase.

The internal canaries are now pipeline/controller regression fixtures only.
Controlled simulated costs are never billed-cost evidence, and historical
`.runs/phase8/stage1-canary` output must not enter formal savings or accepted
rate reports.

## Current benchmark direction

Formal evaluation uses external public Lean benchmarks with:

- frozen source revision, split, license and statement provenance;
- benchmark-specific Lean/Mathlib project configuration;
- eligibility checks completed before model runs;
- ground-truth proof isolation from prompts and retrieval;
- identical checker, safety, trace and cost schemas across arms;
- action-mask baselines that isolate richer action-space effects;
- paired repeated runs and measurement-coverage gates.

The full evaluation contract and remaining work are recorded in
[`docs/development-roadmap.md`](../docs/development-roadmap.md).

## Existing live arm compatibility

The historical action arms still map to explicit runtime configuration:

- `A2`: static action costs, remaining-budget admission disabled;
- `A3`: frozen empirical costs, remaining-budget admission disabled;
- `A4`: frozen empirical costs, remaining-budget admission enabled;
- `A5`: `A4` plus cheap/strong routing;
- `A6`: `A4` with one cheap model and routing disabled.

Empirical arms require `--cost-history-snapshot`. The trace records the actual
`action_runtime_config`; provenance labels alone are not evidence that an
ablation executed.
