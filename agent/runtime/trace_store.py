"""JSONL trace persistence for controller runs."""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ..proof_system.base import (
    CandidateEdit,
    CheckResult,
    ParsedFeedback,
    ProgressSignal,
    ProofTask,
)
from ..search.budget import BudgetSnapshot
from ..search.controller import AttemptRecord, ControllerResult


logger = logging.getLogger(__name__)


class JsonlTraceStore:
    """Append controller results as replay-friendly JSONL events."""

    def __init__(self, path: str | Path, *, include_raw_output: bool = False) -> None:
        self.path = Path(path)
        self.include_raw_output = include_raw_output

    def append_result(self, result: ControllerResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with self.path.open("a", encoding="utf-8") as handle:
            for event in result_events(result, include_raw_output=self.include_raw_output):
                handle.write(
                    json.dumps(event, ensure_ascii=False, sort_keys=True, default=_json_default)
                )
                handle.write("\n")
                count += 1
        logger.info(
            "Appended trace events: path=%s task_id=%s events=%d include_raw_output=%s",
            self.path,
            result.task.task_id,
            count,
            self.include_raw_output,
        )


def result_events(
    result: ControllerResult,
    *,
    include_raw_output: bool = False,
) -> Iterable[dict[str, Any]]:
    """Convert a controller result into JSONL event dictionaries."""

    run_id = _run_id(result)
    yield {
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
        "metadata": result.metadata,
    }
    for attempt in result.attempts:
        yield {
            "event": "attempt",
            "run_id": run_id,
            "task_id": result.task.task_id,
            "attempt": _attempt_payload(attempt, include_raw_output=include_raw_output),
        }


def _run_id(result: ControllerResult) -> str:
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
        "progress": _progress_payload(result.progress) if result.progress is not None else None,
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
    }


def _progress_payload(progress: ProgressSignal) -> dict[str, Any]:
    return {
        "accepted_prefix_chars": progress.accepted_prefix_chars,
        "goal_count_delta": progress.goal_count_delta,
        "goal_size_delta": progress.goal_size_delta,
        "diagnostic_category": progress.diagnostic_category.value,
        "introduced_obligations": progress.introduced_obligations,
        "moved_to_semantic_obligation": progress.moved_to_semantic_obligation,
        "features": progress.features,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)
