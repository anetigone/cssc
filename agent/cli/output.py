"""Output formatting helpers for the Lean task-solving CLI."""

from __future__ import annotations

from agent import ControllerResult, ProofTask


def task_summary(task: ProofTask, index: int) -> dict[str, object]:
    return {
        "index": index,
        "task_id": task.task_id,
        "task_name": task.metadata.get("task_name"),
        "source_file": task.metadata.get("source_file"),
        "hole_kind": task.metadata.get("hole_kind"),
        "hole_line": task.metadata.get("hole_line"),
        "hole_column": task.metadata.get("hole_column"),
        "source_hole_count": task.metadata.get("source_hole_count"),
    }


def result_payload(result: ControllerResult, *, include_candidate_file: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": result.accepted,
        "task_id": result.task.task_id,
        "task_name": result.task.metadata.get("task_name"),
        "stop_reason": result.stop_reason,
        "attempts": len(result.attempts),
        "checks_used": result.budget.checks_used,
        "model_calls_used": result.budget.model_calls_used,
    }
    if result.accepted_attempt is not None and include_candidate_file:
        payload["accepted_candidate_file"] = str(result.accepted_attempt.candidate_file)
    if result.accepted_attempt is not None:
        payload["accepted_proof"] = result.accepted_attempt.edit.text
    problem = result.task.metadata.get("natural_language_problem")
    if isinstance(problem, str) and problem:
        payload["natural_language_problem"] = problem
    informal_proof = result.task.metadata.get("natural_language_proof")
    if isinstance(informal_proof, str) and informal_proof:
        payload["natural_language_proof"] = informal_proof
    if result.attempts:
        last = result.attempts[-1].check_result
        payload["last_category"] = last.category.value
        payload["last_message"] = last.parsed_feedback.message if last.parsed_feedback else ""
    sequence = result.metadata.get("task_sequence")
    if isinstance(sequence, (list, tuple)):
        payload["task_sequence"] = sequence
        payload["task_sequence_complete"] = bool(
            result.metadata.get("task_sequence_complete")
        )
        payload["sequence_checks_used"] = sum(
            int(item.get("checks_used", 0))
            for item in sequence
            if isinstance(item, dict)
        )
        payload["sequence_model_calls_used"] = sum(
            int(item.get("model_calls_used", 0))
            for item in sequence
            if isinstance(item, dict)
        )
    return payload
