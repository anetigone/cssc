"""JSONL trace persistence for controller runs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ..proof_system.base import (
    CandidateEdit,
    CheckResult,
    ParsedFeedback,
    ProofTask,
)
from ..search.budget import BudgetSnapshot
from ..search.controller import AttemptRecord, ControllerResult
from ..search.metrics import RunMetrics, run_metrics_payload


logger = logging.getLogger(__name__)


class JsonlTraceStore:
    """Append controller results as replay-friendly JSONL events."""

    def __init__(self, path: str | Path, *, include_raw_output: bool = False) -> None:
        self.path = Path(path)
        self.include_raw_output = include_raw_output

    def append_result(self, result: ControllerResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(event, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n"
            for event in result_events(result, include_raw_output=self.include_raw_output)
        ]
        _atomic_append_text(self.path, "".join(lines))
        count = len(lines)
        logger.info(
            "Appended trace events: path=%s task_id=%s events=%d include_raw_output=%s",
            self.path,
            result.task.task_id,
            count,
            self.include_raw_output,
        )


def _atomic_append_text(path: Path, text: str) -> None:
    """Append via same-directory replacement, leaving either old or new data.

    A normal append can be interrupted after writing only part of a JSON line.
    Here the existing file and new payload are assembled in a temporary file,
    flushed to disk, and then installed with an atomic filesystem replacement.
    """
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as target:
            if path.exists():
                with path.open("rb") as source:
                    shutil.copyfileobj(source, target)
            target.write(text.encode("utf-8"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def result_events(
    result: ControllerResult,
    *,
    include_raw_output: bool = False,
) -> Iterable[dict[str, Any]]:
    """Convert a controller result into JSONL event dictionaries."""

    run_id = _run_id(result)
    workspace = workspace_payload(result.metadata.get("workspace"))
    summary: dict[str, Any] = {
        "event": "run_summary",
        "run_id": run_id,
        "task": _task_payload(result.task),
        "accepted": result.accepted,
        "stop_reason": result.stop_reason,
        "attempt_count": len(result.attempts),
        "accepted_attempt_index": (
            result.accepted_attempt.attempt_index if result.accepted_attempt is not None else None
        ),
        "budget": _budget_payload(result.budget),
        "metrics": _metrics_payload(result.metrics),
        "metadata": _summary_metadata(result.metadata),
    }
    # Structured-mode runs attach a serialized ProofWorkspace under
    # ``metadata["workspace"]``; minimal runs omit it. The payload is already a
    # plain dict, so the trace store stays unaware of the workspace types.
    if workspace is not None:
        summary["workspace_event"] = "workspace_snapshot"
    yield summary
    if workspace is not None:
        yield {
            "event": "workspace_snapshot",
            "run_id": run_id,
            "task_id": result.task.task_id,
            "workspace": workspace,
        }
    for attempt in result.attempts:
        yield {
            "event": "attempt",
            "run_id": run_id,
            "task_id": result.task.task_id,
            "attempt": _attempt_payload(attempt, include_raw_output=include_raw_output),
        }


def _run_id(result: ControllerResult) -> str:
    """Stable per-run identifier.

    Prefers the unique ``sample_id`` carried by the run metrics so two
    independent runs of the same task — which can collide on task id, attempt
    count and stop reason — still get distinct run ids. Falls back to the
    legacy composite only when metrics are absent.
    """
    if result.metrics is not None and result.metrics.sample_id:
        return result.metrics.sample_id
    return f"{result.task.task_id}:{len(result.attempts)}:{result.stop_reason}"


def _task_payload(task: ProofTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "hole_marker": task.hole_marker,
        "imports": list(task.imports),
        "metadata": task.metadata,
    }


def _budget_payload(snapshot: BudgetSnapshot) -> dict[str, Any]:
    return {
        "checks_used": snapshot.checks_used,
        "model_calls_used": snapshot.model_calls_used,
        "elapsed_seconds": snapshot.elapsed_seconds,
        "remaining_checks": snapshot.remaining_checks,
        "remaining_model_calls": snapshot.remaining_model_calls,
        "exhausted_reason": snapshot.exhausted_reason,
    }


def _metrics_payload(metrics: RunMetrics | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    return run_metrics_payload(metrics)


def _summary_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key != "workspace"}


def workspace_payload(workspace: Any) -> dict[str, Any] | None:
    """Pass through a structured-run workspace snapshot, or ``None``.

    The producer (a structured controller, added in a later phase) is expected
    to place an already-serialized :class:`ProofWorkspace` dict under
    ``ControllerResult.metadata["workspace"]``. Keeping serialization on the
    producer side means the trace store imports no workspace types, so the
    minimal loop's dependency graph is unchanged. Returns ``None`` for missing
    or falsy payloads so the run_summary omits the key for minimal runs.
    """
    if not workspace:
        return None
    if isinstance(workspace, dict):
        return workspace
    # A live workspace object is not expected here; coerce defensively rather
    # than crash the trace write.
    return str(workspace)


def _attempt_payload(
    attempt: AttemptRecord,
    *,
    include_raw_output: bool,
) -> dict[str, Any]:
    return {
        "attempt_index": attempt.attempt_index,
        "candidate_id": attempt.candidate_id,
        "candidate_file": str(attempt.candidate_file),
        "edit": _edit_payload(attempt.edit),
        "check_result": _check_result_payload(
            attempt.check_result,
            include_raw_output=include_raw_output,
        ),
    }


def _edit_payload(edit: CandidateEdit) -> dict[str, Any]:
    return {
        "text": edit.text,
        "action": edit.action,
        "parent_node_id": edit.parent_node_id,
        "metadata": edit.metadata,
    }


def _check_result_payload(
    result: CheckResult,
    *,
    include_raw_output: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "accepted": result.accepted,
        "category": result.category.value,
        "candidate_file": str(result.candidate_file) if result.candidate_file else None,
        "command": list(result.command),
        "exit_code": result.exit_code,
        "elapsed_seconds": result.elapsed_seconds,
        "parsed_feedback": (
            _feedback_payload(result.parsed_feedback) if result.parsed_feedback is not None else None
        ),
    }
    if include_raw_output:
        payload["raw_output"] = result.raw_output
    return payload


def _feedback_payload(feedback: ParsedFeedback) -> dict[str, Any]:
    return {
        "category": feedback.category.value,
        "message": feedback.message,
        "line": feedback.line,
        "column": feedback.column,
        "unsolved_goals": list(feedback.unsolved_goals),
        "goal_state": [
            {
                "text": state.text,
                "goal_fingerprint": state.goal_fingerprint,
                "declaration_id": state.declaration_id,
                "source_span": list(state.source_span) if state.source_span else None,
                "is_sorry_goal": state.is_sorry_goal,
            }
            for state in feedback.goal_state
        ],
    }

def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)
