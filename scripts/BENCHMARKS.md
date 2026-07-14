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

## miniF2F external preparation

The Google DeepMind Lean 4 miniF2F checkout is external data and must not be
committed to this repository.  The entire repository-local `benchmark/`
directory is ignored by Git.  Source data and prepared data are kept separate:

```text
benchmark/
├── miniF2F/                 # upstream checkout; treat as read-only
└── generated/miniF2F/       # scaffolds, manifest and provenance
```

`.runs/` is reserved for actual experiment traces, cost records and final
results; benchmark source/preparation data must not be written there.

Prepare the two upstream aggregate files as 488 independent one-hole tasks:

```bash
python scripts/minif2f_prepare.py prepare
python scripts/minif2f_prepare.py validate
```

Preparation is deliberately offline: it does not install a Lean toolchain,
download Lake packages, or invoke the checker.  It performs these checks:

- the external checkout has the expected Lean 4 project files;
- `Valid.lean` and `Test.lean` yield 244 canonical tasks each;
- `.variants.` declarations are excluded;
- task ids are unique and the two splits do not overlap;
- upstream proof bodies are removed, including the few non-`sorry` proofs;
- every generated scaffold round-trips through `LeanTaskBuilder` as one hole;
- statement, scaffold, source, license, revision, toolchain and dependency
  provenance are recorded with hashes.

The generated manifest marks every task as `eligibility: not_checked` until a
separate benchmark-project Lean smoke is implemented.  Do not interpret
offline preparation as checker eligibility.

After installing the benchmark's pinned Lean/Lake environment, run the real
per-task eligibility gate:

```bash
python scripts/minif2f_eligibility.py
```

It replaces only the proof marker with `sorry`, audits every statement for
references to other benchmark task identifiers, then checks one aggregate per
split.  With zero cross-task references, preceding ordinary theorem
declarations cannot satisfy a later statement or alter its environment. Failed
batches are recursively bisected down to individual tasks. Results are stored under
`benchmark/generated/miniF2F/eligibility_runs/`; the ignored manifest and
provenance receive the latest per-task eligibility status.  This gate proves
statement elaboration only, not proof acceptance or safety.

After an adapter-only repair, `--reuse-results <prior-results.jsonl>` can reuse
only prior `eligible` evidence whose materialized candidate SHA-256 is
unchanged. Changed tasks are checked again; failures and infrastructure results
are never reused as success.

## miniF2F execution

Do not invoke `python -m agent.cli prove` 488 times.  The suite runner keeps one
Python process and one required Lean language server alive across all selected
tasks, while creating a fresh controller, budget, trace and result for each
task. It never silently falls back to a cold subprocess checker.

Start with the public validation split and a named pilot:

```bash
python scripts/minif2f_benchmark_run.py \
  --split valid --limit 5 --run-name minif2f-valid-pilot \
  -- --use-model --max-model-calls 3 --max-checks 3 \
  --lean-timeout 300 --lean-server-startup-timeout 300
```

After freezing the configuration, run the full split by removing `--limit`.
Run `test` only with that frozen configuration. Ordinary `cssc prove` options
go after `--`; benchmark selection and output options go before it.

Outputs are written under `.runs/benchmarks/minif2f/<run-name>/`. Each task has
its own `result.json`, `trace.jsonl`, and candidate directory. `run.json` pins
the benchmark revision, selected task ids and proof arguments; `summary.json`
is updated atomically after every task.

Resume an interrupted run without re-running completed tasks:

```bash
python scripts/minif2f_benchmark_run.py \
  --split valid --limit 5 \
  --resume .runs/benchmarks/minif2f/minif2f-valid-pilot \
  -- --use-model --max-model-calls 3 --max-checks 3 \
  --lean-timeout 300 --lean-server-startup-timeout 300
```

The selection and all proof arguments must match the original run exactly.
By default the suite stops at the first checker infrastructure failure so the
same run can be repaired and resumed without spending model budget on later
tasks.

After repairing a provider or checker outage, rerun only infrastructure results
(including older `generation:provider_error` results that were initially
misclassified):

```bash
python scripts/minif2f_benchmark_run.py \
  --split valid --limit 5 \
  --resume .runs/benchmarks/minif2f/minif2f-valid-pilot \
  --retry-infrastructure-failures \
  -- --use-model --max-model-calls 3 --max-checks 3 \
  --lean-timeout 300 --lean-server-startup-timeout 300
```

`failed` counts proof/check failures only; infrastructure failures are reported
separately and are excluded from the accepted-rate denominator.

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
