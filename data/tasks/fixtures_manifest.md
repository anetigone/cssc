# Phase 0 Baseline Fixtures

These fixtures fix the evaluation vocabulary for the proof-system redesign.
Every later phase is ablated against the same baseline, so the fixtures and the
per-run metrics (see `agent/search/metrics.py`) must stay stable.

## Fixture groups

### `simple` — `data/tasks/fixtures_simple.json`

Single-obligation tasks whose failures are parser, API, typing, or local
tactic problems. The minimal refinement core is **expected to solve these**.
These anchor the lower bound: if a change regresses these, it is not ready for
the default execution path.

### `complex` — `data/tasks/fixtures_complex.json`

Multi-obligation or capability-bound tasks. They trigger the conditions in
Section 1.2 of the plan (repeated same-goal stalls, missing intermediate
lemmas, mutually-dependent goals). The minimal core is **expected to stall**,
producing a structured stagnation report — not to solve them. These anchor the
upper end where `structured` mode is meant to add measurable value.

Each complex task carries a `why_complex` field documenting the failure mode it
is meant to expose, and an `expected_outcome` of `stall_or_repeated_goal`.
These are descriptive annotations for the eval harness author, **not** Lean
checker categories — the actual pass/stall signal comes from the run metrics,
not from these fields.

### `explicit_function.md` — `data/tasks/explicit_function.md`

Hand-curated natural-language fixture (modified Bessel function analysis).
Exercises the formalize → prove path end to end on a genuinely hard, real
problem. Used as a qualitative smoke target, not a pass/fail metric.

## Per-run metric contract

Every run records, for each attempt:

- `attempt_index`, `action`, checker `category`, `accepted`;
- `goal_fingerprints` — the **full ordered set** of unsolved goals for that
  attempt, each hashed with `goal_fingerprint` (stable against whitespace /
  line noise). The full set is captured, not just the first goal, so a
  multi-obligation task can show goal B repeating while goal A was discharged;
- `solved_goals` / `retained_goals` / `introduced_goals` — the set delta
  against the **previous attempt's** goal set, plus `goal_count_delta`;
- `progressed` — derived **only** from the goal-set delta (an attempt that
  discharges a goal without reintroducing one), never from the error category.
  Repeated failures on the same goal correctly record `progressed=false`;
- `elapsed_seconds`.

The run roll-up (`RunMetrics`) records:

- `sample_id` — a unique id per independent run (generated at run start). This
  is the grouping key for evaluation, replacing the collision-prone
  `task_id:attempt_count:stop_reason` run id in traces;
- `accepted` — the only success outcome;
- `stop_reason` — clean accept vs `budget:*` vs `no_actions` vs
  `tool_unavailable`;
- `distinct_goal_fingerprints` and `repeated_goal_stalls` — whether the loop is
  exploring new goals or grinding on the same one (a retained goal across
  attempts counts as one stall).

pass@k is **not** a single-run property. It is computed by
`EvaluationAggregator.pass_at_k`, which takes k independent `RunMetrics`
samples of one task and returns a `PassAtKResult`. A single iterative
controller run is one sample; running the task k times yields k samples with
distinct `sample_id`s. Recording pass@k on a run would let one iterative run
masquerade as k independent attempts, so it is deliberately absent from
`RunMetrics` and `ControllerConfig`.

These metrics are emitted into the JSONL trace `run_summary` event under the
`metrics` key (and the `run_id` is now the `sample_id`), alongside the
existing `attempt` events.
