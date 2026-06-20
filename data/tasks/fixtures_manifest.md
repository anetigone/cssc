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

### `explicit_function.md` — `data/tasks/explicit_function.md`

Hand-curated natural-language fixture (modified Bessel function analysis).
Exercises the formalize → prove path end to end on a genuinely hard, real
problem. Used as a qualitative smoke target, not a pass/fail metric.

## Per-run metric contract

Every run records, for each attempt:

- `attempt_index`, `action`, checker `category`, `accepted`;
- `goal_fingerprint` (stable hash of the first unsolved goal, see
  `goal_fingerprint`);
- `progressed` (derived from the adapter `ProgressSignal`);
- `elapsed_seconds`.

The run roll-up (`RunMetrics`) records:

- `accepted` (the only success outcome);
- `stop_reason` (clean accept vs `budget:*` vs `no_actions` vs
  `tool_unavailable`);
- `pass_at_k` (defaults to `1` for a single iterative run; independent repeats
  set it explicitly so "stuck but iterating" is never scored as pass@k);
- `distinct_goal_fingerprints` and `repeated_goal_stalls` (whether the loop is
  exploring new goals or grinding on the same one).

These metrics are emitted into the JSONL trace `run_summary` event under the
`metrics` key, alongside the existing `attempt` events.
