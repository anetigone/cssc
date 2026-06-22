"""Run state and result construction for the structured controller.

Parallel to ``agent/search/controller/results.py`` but built on the structured
``_StructuredRunState``: the structured loop's authoritative state lives in the
:class:`ProofWorkspace`, so the run state only accumulates the shared
attempt/metric/safety observations that flow into :class:`RunMetrics` and the
trace. We reuse :func:`summarize_run` / :func:`new_sample_id` from the common
metrics module so structured and minimal runs produce identical observation
fields for cross-mode comparison.

We deliberately do not refactor ``results.py``: its builders are coupled to the
minimal ``_ControllerRunState`` (linear feedback history, self-managed memory),
and sharing them would risk the minimal path. Keeping a parallel builder here
is the lower-risk choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..budget import BudgetManager
from ..controller.types import AttemptRecord, ControllerResult
from ..execution import ExecutionMode
from ..metrics import new_sample_id, summarize_run

if TYPE_CHECKING:
    from ...proof_system.base import ProofTask
    from ...proof_system.workspace import ProofWorkspace
    from ..safety import SafetyReviewer


@dataclass
class _StructuredRunState:
    """Mutable accumulator for one structured run's shared observations.

    The authoritative search state is the :class:`ProofWorkspace`; this object
    only holds the attempt stream and safety rejections that the Phase 0
    metrics layer and the trace need regardless of execution mode.
    """

    attempts: list[AttemptRecord] = field(default_factory=list)
    attempt_metrics: list = field(default_factory=list)
    attempt_index: int = 0
    stop_reason: str = "budget"
    sample_id: str = field(default_factory=new_sample_id)
    safety_rejections: list[dict[str, Any]] = field(default_factory=list)
    feedback_history: list = field(default_factory=list)
    current_retrieved: tuple = ()
    retrieved_history: list = field(default_factory=list)
    retrieved_this_iteration: bool = False


def build_structured_result(
    state: _StructuredRunState,
    task: ProofTask,
    workspace: ProofWorkspace,
    *,
    accepted: bool,
    stop_reason: str,
    execution_mode: ExecutionMode,
    budget: BudgetManager,
    safety_reviewer: SafetyReviewer,
) -> ControllerResult:
    """Construct the :class:`ControllerResult` for one structured run.

    Mirrors ``build_final_result`` / ``build_accepted_result``: same fields,
    same metrics roll-up, plus the serialized workspace under
    ``metadata["workspace"]`` so :func:`trace_store.workspace_payload` surfaces
    it in the run summary.
    """
    snapshot = budget.snapshot()
    metrics = summarize_run(
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
    accepted_attempt = (
        state.attempts[-1] if accepted and state.attempts else None
    )
    metadata: dict[str, Any] = {
        "workspace": workspace.to_dict(),
        "safety_rejections": tuple(state.safety_rejections),
        "safety_reviewer": type(safety_reviewer).__name__,
    }
    return ControllerResult(
        task=task,
        accepted=accepted,
        attempts=tuple(state.attempts),
        budget=snapshot,
        stop_reason=stop_reason,
        accepted_attempt=accepted_attempt,
        metrics=metrics,
        metadata=metadata,
    )
