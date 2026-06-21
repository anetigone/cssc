"""Result builders and Phase-0 metric roll-up for the controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..budget import BudgetManager
from ..execution import ExecutionMode
from ..memory import memory_to_dict
from ..metrics import RunMetrics, summarize_run
from .types import AttemptRecord, ControllerResult, _ControllerRunState

if TYPE_CHECKING:
    from ...proof_system.base import ProofTask
    from ..safety import SafetyReviewer


logger = logging.getLogger(__name__)


def result_metadata(
    state: _ControllerRunState,
    safety_reviewer: SafetyReviewer,
) -> dict[str, Any]:
    """Shared metadata block recorded on every controller result.

    Includes a snapshot of the final self-managed memory so the trace preserves
    the compact context the loop actually carried, alongside the raw Phase 0
    fields. The memory snapshot is a plain dict, never the live object, so it
    serializes cleanly.
    """
    return {
        "retrieved_results": tuple(state.retrieved_history),
        "feedback_count": len(state.feedback_history),
        "proof_memory": memory_to_dict(state.memory),
        "safety_rejections": tuple(state.safety_rejections),
        "safety_reviewer": type(safety_reviewer).__name__,
    }


def run_metrics(
    state: _ControllerRunState,
    task: ProofTask,
    *,
    accepted: bool,
    stop_reason: str,
    execution_mode: ExecutionMode,
    budget: BudgetManager,
) -> RunMetrics:
    """Build the Phase 0 baseline roll-up for the current run state."""
    snapshot = budget.snapshot()
    return summarize_run(
        sample_id=state.sample_id,
        task_id=task.task_id,
        accepted=accepted,
        stop_reason=stop_reason,
        attempts=state.attempt_metrics,
        budget_checks_used=snapshot.checks_used,
        budget_model_calls_used=snapshot.model_calls_used,
        budget_exhausted_reason=snapshot.exhausted_reason,
        execution_mode=execution_mode,
    )


def build_accepted_result(
    state: _ControllerRunState,
    task: ProofTask,
    record: AttemptRecord,
    budget: BudgetManager,
    execution_mode: ExecutionMode,
    safety_reviewer: SafetyReviewer,
) -> ControllerResult:
    """Construct the result for an accepted-and-safe candidate."""
    logger.info(
        "Controller accepted proof: task_id=%s attempt_index=%d",
        task.task_id,
        record.attempt_index,
    )
    return ControllerResult(
        task=task,
        accepted=True,
        attempts=tuple(state.attempts),
        budget=budget.snapshot(),
        stop_reason="accepted",
        accepted_attempt=record,
        metrics=run_metrics(
            state,
            task,
            accepted=True,
            stop_reason="accepted",
            execution_mode=execution_mode,
            budget=budget,
        ),
        metadata=result_metadata(state, safety_reviewer),
    )


def build_tool_unavailable_result(
    state: _ControllerRunState,
    task: ProofTask,
    budget: BudgetManager,
    execution_mode: ExecutionMode,
    safety_reviewer: SafetyReviewer,
) -> ControllerResult:
    """Construct the result when the checker tool becomes unavailable."""
    logger.warning(
        "Controller stopped: task_id=%s reason=%s",
        task.task_id,
        state.stop_reason,
    )
    return ControllerResult(
        task=task,
        accepted=False,
        attempts=tuple(state.attempts),
        budget=budget.snapshot(),
        stop_reason=state.stop_reason,
        metrics=run_metrics(
            state,
            task,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=execution_mode,
            budget=budget,
        ),
        metadata=result_metadata(state, safety_reviewer),
    )


def build_final_result(
    state: _ControllerRunState,
    task: ProofTask,
    budget: BudgetManager,
    execution_mode: ExecutionMode,
    safety_reviewer: SafetyReviewer,
) -> ControllerResult:
    """Construct the result when the loop exits without an accepted proof."""
    reason = budget.exhausted_reason()
    if reason is not None and not state.stop_reason.startswith("budget:"):
        state.stop_reason = f"budget:{reason}"
    logger.info(
        "Controller run finished: task_id=%s accepted=False stop_reason=%s attempts=%d",
        task.task_id,
        state.stop_reason,
        len(state.attempts),
    )
    return ControllerResult(
        task=task,
        accepted=False,
        attempts=tuple(state.attempts),
        budget=budget.snapshot(),
        stop_reason=state.stop_reason,
        metrics=run_metrics(
            state,
            task,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=execution_mode,
            budget=budget,
        ),
        metadata=result_metadata(state, safety_reviewer),
    )
