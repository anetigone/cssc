# Agent Data Structures and Module Interfaces

This document records the current MVP contracts for the cost-sensitive proof
agent. The design goal is to keep the controller proof-system-aware enough to
use verifier feedback, but not coupled to Lean file layout, command execution,
or generated workspace details.

## Core Invariant

The controller operates on one active editable proof hole at a time.

A source file may contain multiple candidate holes. The task builder can split
that source into multiple `ProofTask` objects, but each emitted task must have
exactly one active `hole_marker` in `source_template`. Other holes are made
inactive during extraction, currently by filling them with `sorry`.

This keeps rendering, verification, blame assignment, caching, and trace
comparison simple while leaving room for multi-hole source extraction later.

## Data Structures

### `ProofTask`

Defined in `agent/proof_system_adapter.py`.

```text
task_id: stable task identifier
source_template: full source with exactly one active hole marker
hole_marker: marker string replaced by a candidate edit
imports: extra imports to prepend at render time
metadata: provenance, split, hole location, retrieval policy, leakage guards
```

The task is the dataset-facing object. It should be serializable and should not
contain transient checker output or search-tree state.

Important metadata keys produced by `LeanTaskBuilder`:

```text
proof_system: "lean4"
source_file: absolute path when available
split: train/dev/test-style split name
hole_kind: "marker" or "sorry"
hole_id: stable local hole identifier
hole_index: index among source holes
hole_line, hole_column: 1-based source location
hole_start, hole_end: character offsets in the original source
original_hole_text: original text replaced during extraction
active_hole_count: always 1 for controller-facing tasks
source_hole_count: number of candidate holes found in the source
inactive_hole_fill: text used to deactivate non-target source holes
has_inactive_holes: whether this task came from a multi-hole source
source_imports: imports parsed from the source file
ground_truth_hidden: retrieval/evaluation leakage guard
allowed_retrieval_scope: retrieval policy hint
```

### `CandidateEdit`

Defined in `agent/proof_system_adapter.py`.

```text
text: replacement for the active proof hole
action: meta-action that produced the edit, such as manual, expand, repair
parent_node_id: optional search-tree parent
metadata: proposer-specific details, confidence, prompts, retrieval ids
```

Candidate edits are intentionally single-hole patches in the MVP. Whole-file
rewrites should be represented later as a different edit type rather than
overloading this one.

### `BudgetSlice`

Defined in `agent/proof_system_adapter.py`.

```text
timeout_seconds: checker wall-clock budget for one verification call
```

This is deliberately small for now. Future budget fields can include model
tokens, dollar cost, retrieval count, checker retries, or model tier.

### `CheckResult`, `ParsedFeedback`, and `ProgressSignal`

Defined in `agent/proof_system_adapter.py`.

`CheckResult` is the verifier-facing result of one materialized candidate. It
contains acceptance, normalized category, raw output, command metadata, timing,
parsed feedback, and progress features.

`ParsedFeedback` converts raw prover output into controller-usable categories
such as parser error, unknown identifier, type mismatch, unsolved goals, tactic
failure, timeout, and accepted proof.

`ProgressSignal` is the thin bridge from verifier feedback to cost-sensitive
search. It should contain cheap features that help rank whether another
expansion, repair, retrieval step, or backtrack is worth its cost.

## Module Boundaries

### Task Builder

Current implementation: `agent/task_builder.py`.

Responsibilities:

- extract `ProofTask` objects from Lean source;
- preserve provenance and leakage metadata;
- emit one active-hole task per selected source hole;
- avoid Lean execution and search decisions.

Non-responsibilities:

- checking whether a proof is valid;
- selecting which task to solve next;
- exposing ground-truth proof text to proposers or retrievers during test runs.

Current extraction modes:

```text
explicit marker: uses {{proof}} by default
standalone sorry: tokenizer ignores comments and strings
```

Multiple source holes are opt-in through `TaskBuilderConfig`:

```text
allow_multiple_marker_tasks=True
allow_multiple_sorry_tasks=True
inactive_hole_fill="sorry"
```

When inactive holes are filled with `sorry`, the resulting task is useful for
local extraction and candidate generation, but a strict checker policy that
rejects any remaining `sorry` will not mark the whole file accepted. For final
evaluation, either fill inactive holes with trusted proof text or use a checker
policy that explicitly distinguishes inactive holes from the active candidate.

### Proof System Adapter

Current interface: `ProofSystemAdapter` in `agent/proof_system_adapter.py`.
Current backend: `LeanAdapter` in `agent/lean_adapter.py`.

Responsibilities:

- render a `ProofTask` and `CandidateEdit` into complete source;
- run the checker under a `BudgetSlice`;
- normalize raw checker feedback;
- extract lightweight progress signals.

The controller should depend on `ProofSystemAdapter`, not on `lake`, `lean`,
temporary paths, or Lean diagnostic formatting.

### Attempt Workspace

Current implementation: `agent/workspace.py`.

Responsibilities:

- materialize rendered candidate files in generated directories;
- assign deterministic candidate ids from task id, parent node, action, and edit;
- keep original task files untouched.

The workspace owns generated files. The adapter owns checking. The controller
owns search state and budget decisions.

### Controller, Actions, and Budget

Current implementation:

```text
agent/action.py
agent/budget.py
agent/controller.py
```

`ActionGenerator` is the controller-facing model/proposer boundary. It accepts
an `ActionGenerationRequest` with the task, attempt index, previous feedback,
and requested candidate count, then returns `ActionCandidate` objects that can
be converted into `CandidateEdit`.

The next controller should distinguish model tiers at the action boundary. A
cheap generator should produce short tactic candidates and lightweight repairs.
A strong generator should be invoked only by explicit escalation actions, where
the requested output is a detailed proof completion or decomposition for a
high-value branch. The two tiers may share the same `ActionCandidate` return
shape, but their costs, prompts, and intended roles should be recorded in
metadata.

`BudgetManager` tracks coarse spending:

```text
max_checks
max_model_calls
max_cheap_model_calls
max_strong_model_calls
per_check_timeout_seconds
max_elapsed_seconds
```

`ProofController` currently runs a minimal sequential loop:

```text
generate action candidates
render one candidate edit into source
materialize the source in AttemptWorkspace
run ProofSystemAdapter.check
stop on accepted proof, no actions, tool unavailable, or budget exhaustion
```

This is intentionally not a tree search yet. It is the smallest working chain
that can connect a model, Lean, generated candidate files, and budget accounting.
Future cost-sensitive policy can grow behind the same `ActionGenerator`,
`BudgetManager`, and `ProofSystemAdapter` boundaries.

Expected later actions:

```text
expand_cheap_tactic
repair_cheap
retrieve
escalate_detailed_proof
escalate_decompose
backtrack
prune
stop
```

The controller should treat `escalate_detailed_proof` and
`escalate_decompose` as high-cost macro-actions rather than ordinary retries.
They should be chosen when verifier progress, retrieved context, branch value,
or cheap-attempt saturation indicates that a strong-model call has enough
expected value per unit cost.

## Extension Notes

### Multi-Hole Source Files

The current path is source-level multi-hole extraction, not multi-hole candidate
edits. A file with several `sorry` or `{{proof}}` markers becomes several
single-active-hole `ProofTask` objects. This keeps the rest of the MVP stable.

True multi-hole solving should be added as a separate task/edit contract, for
example `CompositeProofTask` plus an edit map from `hole_id` to replacement
text. Do not silently change `CandidateEdit.text` to mean multiple edits.

### Ground Truth

Ground-truth proof text may be useful for benchmark construction and evaluation,
but it should not be visible to the proposer, retriever, or repair agent during
test runs. Keep leakage policy in task metadata and enforce it in retrieval and
prompt construction.

### Prover Portability

The MVP is Lean-first. The abstraction boundary exists to keep the search
controller clean, not to pretend Isabelle or another prover is already
implemented. Add prover-neutral fields only when the controller actually uses
them.
