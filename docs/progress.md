# Progress Log

## 2026-06-15

### Architecture

- Revised the README architecture toward a Lean-first MVP.
- Added an explicit `ProofSystemAdapter` boundary so the controller is not tied
  to Lean command execution or diagnostic formatting.
- Kept the framework deliberately small: one active proof hole per task,
  sequential controller loop, coarse budget accounting, and OpenAI-compatible
  model access through one adapter.

### Implemented Modules

- `agent/proof_system_adapter.py`
  - Shared task, candidate, budget slice, checker result, feedback, and progress
    data structures.
  - Abstract `ProofSystemAdapter` contract.

- `agent/lean_adapter.py`
  - Lean 4 checker adapter.
  - Uses `lake env lean` inside Lake projects when available, otherwise `lean`.
  - Structured handling for missing Lean, missing elan toolchain, timeouts,
    parser errors, unknown identifiers, type mismatches, unsolved goals, tactic
    failures, and `sorry` warnings.

- `agent/task_builder.py`
  - Builds `ProofTask` objects from explicit `{{proof}}` markers or standalone
    `sorry` holes.
  - Ignores `sorry` inside comments and strings.
  - Defaults to one active hole per task; multi-hole extraction is opt-in.

- `agent/workspace.py`
  - Deterministic generated candidate workspace.
  - Writes rendered Lean candidate files without modifying source tasks.

- `agent/action.py`
  - `ActionGenerator` protocol.
  - `ActionCandidate` and deterministic `StaticActionGenerator` for tests and
    smoke runs.

- `agent/budget.py`
  - `BudgetManager` with model-call, checker-call, per-check timeout, and
    elapsed-time limits.

- `agent/controller.py`
  - Minimal sequential `ProofController`.
  - Generates candidates, renders them, materializes files, checks with the
    proof adapter, records attempts, and stops on accepted proof or budget/tool
    limits.

- `agent/model_adapter.py`
  - OpenAI-compatible chat completions action generator.
  - Works with DeepSeek-style OpenAI-compatible APIs through configuration.
  - No per-provider adapter is needed while the provider supports the same chat
    completions shape.

- `agent/env_loader.py`
  - Lightweight `.env` loader for local runs.
  - Supports comments, `export`, quoted values, inline comments, and preserves
    existing environment variables by default.

- `scripts/smoke_openai_controller.py`
  - End-to-end smoke script for model -> controller -> Lean.
  - Supports mock model mode and real API mode.
  - Supports `true` and `and_comm` smoke tasks.

### Tests

- Added unit tests for:
  - Lean adapter rendering, missing tool handling, feedback parsing, and
    optional real Lean checks.
  - Task builder extraction and JSONL export.
  - Action candidates and static generation.
  - Budget accounting and exhaustion.
  - Controller accepted path, budget exhaustion, and no-action stopping.
  - OpenAI-compatible model adapter response parsing and URL construction.
  - `.env` loading behavior.

### Verification

- `python -m unittest discover -s tests -v`
  - 27 tests pass.
  - 2 real Lean tests skip inside the sandbox when elan toolchain access is not
    available.

- `python -m compileall agent tests scripts`
  - Passes.

- `python scripts\smoke_openai_controller.py --mock-model --task and_comm --lean-timeout 60`
  - Passes in non-sandbox mode with real Lean.

- `python scripts\smoke_openai_controller.py --task and_comm --lean-timeout 60 --max-checks 1 --max-model-calls 1`
  - Passed once with the real DeepSeek/OpenAI-compatible API configuration from
    `.env`.

### Current Design Notes

- Keep one OpenAI-compatible model adapter for DeepSeek and similar providers.
  Add provider-specific adapters only when a provider does not support the same
  chat completions request/response shape.
- A future `ModelProfile` layer is likely useful for provider/model-specific
  defaults such as temperature, max tokens, prompt style, stop sequences, and
  retry behavior.
- The controller is not yet a tree search. It is a working single-chain loop
  suitable for proving the model/checker integration path.
- Multi-hole source extraction exists, but final strict checking can reject
  inactive holes filled with `sorry`. Proper multi-hole solving should be added
  as a separate task/edit contract.
- `.env` is ignored by git and should remain untracked.

### Suggested Next Steps

- Add `ModelProfile` presets for DeepSeek and other OpenAI-compatible models.
- Persist controller traces as JSONL for later analysis and replay.
- Add a simple repair loop that feeds Lean feedback back into the model for a
  second attempt.

## 2026-06-16

### CLI

- Added the Lean task-solving CLI.
  - Core implementation lives in `agent/cli/solve_lean_task.py`.
  - The repository root keeps a thin `solve_lean_task.py` entry point.
  - Builds `ProofTask` objects from a Lean file or Lean source directory.
  - Supports task discovery through `--list-tasks`.
  - Selects one task with `--task-index` or `--task-id`.
  - Solves the selected task through the existing `ProofController`,
    `LeanAdapter`, `AttemptWorkspace`, and budget configuration.
  - Supports deterministic static candidates with `--candidate` or
    `--candidate-file`.
  - Supports real OpenAI-compatible model calls with `--use-model`, loading
    environment configuration only through the existing `.env` loader.
  - Auto-detects a nearby Lake project root and offers `--project-root` and
    `--no-lake` overrides.

### Tests

- Added CLI helper tests for task building, task selection, static candidate
  loading, `.env` existence behavior, and Lake root detection.

### Verification

- `python -m unittest discover -s tests -v`
  - 32 tests pass.
  - 2 real Lean tests skip inside the sandbox when elan toolchain access is not
    available.

- `python -m compileall agent tests scripts`
  - Passes.

### Suggested Next Steps

- Add `ModelProfile` presets for DeepSeek and other OpenAI-compatible models.
- Persist controller traces as JSONL for later analysis and replay.
- Add a simple repair loop that feeds Lean feedback back into the model for a
  second attempt.
