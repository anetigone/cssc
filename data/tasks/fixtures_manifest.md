# Phase 0 Baseline Inputs

These files provide stable inputs for comparing later proof-system versions.
Phase 0 does not predict whether a model should solve, stall, or require a
particular number of iterations. Those are experimental outcomes recorded by
the trace, not fixture metadata.

## Input groups

### `simple` — `data/tasks/fixtures_simple.json`

Small, single-obligation Lean scaffolds. They are useful for detecting basic
regressions without defining a success threshold in the fixture itself.

### `complex` — `data/tasks/fixtures_complex.json`

Lean scaffolds with multiple logical obligations or less direct mathematical
structure. `structural_feature` documents why an input is useful; it is not an
expected result or checker category.

### `explicit_function.md` — `data/tasks/explicit_function.md`

A hard natural-language modified-Bessel-function problem. It is a qualitative
formalize-and-prove smoke input, not a pass/fail metric.

## Raw observation contract

Every checked candidate records:

- `attempt_index` and `action`;
- checker `category` and `accepted`;
- all captured `goal_fingerprints`, preserving order and multiplicity;
- checker `error_message` and `elapsed_seconds`.

Every controller run records:

- a unique `sample_id` and the `task_id`;
- `accepted` and `stop_reason`;
- checks/model calls used and the budget exhaustion reason;
- the ordered attempt observations.

The baseline intentionally does not derive:

- progress or regression;
- solved, retained, or introduced goals;
- repeated-stall classifications;
- pass@k or other cross-run evaluation statistics.

These require explicit semantics and belong to later evaluation or search
policy work. Raw observations are emitted under `metrics` in the JSONL
`run_summary`; existing detailed `attempt` events remain unchanged.
