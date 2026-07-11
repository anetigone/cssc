# Controlled-track scenarios

Each `*.json` here is one controlled-track scenario for the matching task. The controlled track
(Stage 2) feeds a scripted proposal sequence plus fixed checker verdicts into the structured
controller to prove the implementation's causal chain — it does **not** call a model and is not
counted toward the live success rate.

## Schema

```json
{
  "scenario_version": 1,
  "task_id": "l1_canary_true",
  "description": "...",
  "proposals": [
    { "action": {...}, "payload": {...} }
  ],
  "expected_check_results": [
    {"on_candidate_contains": "...", "accepted": true, "category": "proof_accepted"}
  ]
}
```

### `proposals[*]`

Each item is exactly `StructuredActionProposal.to_dict()`
(`agent/search/structured/proposal/core.py`), so it round-trips through
`structured_action_proposal_from_dict` without any custom schema:

- `action` — `SearchAction.to_dict()`: `kind`, `target_branch_id`, `target_step_ids`,
  `allowed_mutations`, `rationale`. `allowed_mutations` must be a subset of the kind's default
  scope (`DEFAULT_ALLOWED_MUTATIONS` in `agent/proof_system/workspace/action.py`):
  - `implement` / `repair_implementation`: `["lean_artifact", "alignment_link"]`
  - `decompose`: `["new_structure", "obligation_dependency"]`
  - `run_capability_test`: `[]`
  - `change_representation`: `["argument_step", "lean_artifact", "alignment_link"]`
- `payload` — one of six typed payloads keyed by `kind`:
  - `implement`: `{kind, proof_text, source?}`
  - `decompose`: `{kind, children:[{child_id, statement, dependency_ids?}], strategy?}`
  - `run_capability_test`: `{kind, requirement, signature, expected_outcome?}`
  - `change_representation`: `{kind, argument:[ArgumentStepSpec], alignments:[AlignmentSpec], rationale?}`
    — note `argument` + `alignments`, **not** `proof_text`.

### `expected_check_results[*]`

A lightweight oracle for the Stage 2 scripted checker (substring match on the candidate). The
`validate` script only checks that each `category` is a legal `DiagnosticCategory` value
(`agent/proof_system/base.py`). Stage 2 actually replays these.

## Stage 0 note

In Stage 0 the validator only deserializes each `proposals[*]` via
`structured_action_proposal_from_dict` and checks the category enums — it does **not** replay the
scenario through the controller. Replay arrives in Stage 2.
