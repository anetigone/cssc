"""Run state and result construction for the structured controller."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent.proof_system.workspace import ObligationStatus, WorkspaceStatus
from ..budget import BudgetManager
from ..controller.types import AttemptRecord, ControllerResult
from ..cost import cost_vector_from_metrics_and_budget, to_dict
from ..cost_ledger import CostLedger
from ..execution import ExecutionMode
from ..metrics import new_sample_id, summarize_run
from .budget_hints import (
    build_obligation_budget_hints,
    join_borrowed_costs,
)
from .costing import build_cost_summary
from .summary import build_result_summary

if TYPE_CHECKING:
    from agent.proof_system.assembler import AssemblyResult
    from agent.proof_system.base import ProofTask
    from agent.proof_system.workspace import ProofWorkspace
    from ..safety import SafetyReviewer


logger = logging.getLogger(__name__)


@dataclass
class _StructuredRunState:
    """Mutable accumulator for shared run observations."""

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
    skipped_proposals: list[dict[str, Any]] = field(default_factory=list)
    decompose_records: list[dict[str, Any]] = field(default_factory=list)
    argument_records: list[dict[str, Any]] = field(default_factory=list)
    representation_records: list[dict[str, Any]] = field(default_factory=list)
    model_usage: list[dict[str, Any]] = field(default_factory=list)
    generation_failures: list[dict[str, Any]] = field(default_factory=list)
    # Phase 8.4: per-pop frontier priority explanations, in pop order. Captured
    # by the controller from ``Frontier.explanations()`` so the trace records
    # why each branch was scheduled regardless of policy.
    priority_explanations: list = field(default_factory=list)
    # Phase 9 authoritative append-only runtime cost observations.
    cost_ledger: CostLedger = field(default_factory=CostLedger)
    proposal_cache_events: list[dict[str, Any]] = field(default_factory=list)


_SOLVABLE_STATUSES: frozenset[ObligationStatus] = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)


def finalize_workspace_status(
    workspace: ProofWorkspace, *, accepted: bool
) -> WorkspaceStatus:
    """Derive the deterministic terminal status of a structured run."""
    if accepted:
        return WorkspaceStatus.ACCEPTED

    graph = workspace.obligation_graph
    active = graph.active()
    root = graph.root()
    root_accepted = root is not None and root.status == ObligationStatus.ACCEPTED

    has_solvable = any(
        obligation.status in _SOLVABLE_STATUSES for obligation in active
    )
    if not has_solvable and not root_accepted:
        return WorkspaceStatus.BLOCKED

    has_verified_helper = any(
        obligation.obligation_id not in workspace.root_obligation_ids
        and obligation.status == ObligationStatus.ACCEPTED
        for obligation in active
    )
    if has_verified_helper:
        return WorkspaceStatus.PARTIAL

    return WorkspaceStatus.SEARCHING


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
    assembly_outcome: AssemblyResult | None = None,
    frontier_policy: str = "legacy",
) -> ControllerResult:
    """Construct the :class:`ControllerResult` for one structured run.

    Mirrors ``build_final_result`` / ``build_accepted_result``: same fields,
    same metrics roll-up, plus the serialized workspace under
    ``metadata["workspace"]`` so :func:`trace_store.workspace_payload` surfaces
    it in the run summary.

    ``assembly_outcome`` carries the final-assembly result when the run reached
    :meth:`StructuredController._assemble_and_finalize`. It is surfaced two
    ways: the raw ``AssemblyResult.to_dict`` (with its ``errors``) under
    ``metadata["assembly"]`` — which previously was dropped on assembly failure
    — and the derived machine-assertable view under
    ``metadata["result_summary"]``. When the run never reached assembly
    (budget exhaustion, ``no_actions``, ``tool_unavailable``), only the
    ``result_summary`` is written and ``assembly.executed`` is ``False``.
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
        model_input_tokens=sum(
            usage.get("input_tokens", 0) for usage in state.model_usage
        ),
        model_output_tokens=sum(
            usage.get("output_tokens", 0) for usage in state.model_usage
        ),
    )
    accepted_attempt = (
        state.attempts[-1] if accepted and state.attempts else None
    )
    final_status = finalize_workspace_status(workspace, accepted=accepted)
    logger.info(
        "Structured workspace finalized: task_id=%s accepted=%s stop_reason=%s "
        "workspace_status=%s workspace_version=%d",
        task.task_id,
        accepted,
        stop_reason,
        final_status.value,
        workspace.version,
    )
    workspace = workspace.successor(status=final_status)
    metadata: dict[str, Any] = {
        "workspace": workspace.to_dict(),
        "frontier_policy": frontier_policy,
        "priority_explanations": tuple(
            expl.to_dict() for expl in state.priority_explanations
        ),
        "safety_rejections": tuple(state.safety_rejections),
        "safety_reviewer": type(safety_reviewer).__name__,
        "skipped_proposals": tuple(state.skipped_proposals),
        "decompose_records": tuple(state.decompose_records),
        "argument_records": tuple(state.argument_records),
        "representation_records": tuple(state.representation_records),
        "model_usage": tuple(state.model_usage),
        "generation_failures": tuple(state.generation_failures),
        "cost_ledger": state.cost_ledger.to_dict(),
        "proposal_cache_events": tuple(state.proposal_cache_events),
        "cost": to_dict(cost_vector_from_metrics_and_budget(metrics, snapshot)),
        "result_summary": build_result_summary(
            workspace, assembly_result=assembly_outcome
        ).to_dict(),
    }
    if assembly_outcome is not None:
        metadata["assembly"] = assembly_outcome.to_dict()
    cost_summary = build_cost_summary(
        workspace=workspace,
        attempts=tuple(state.attempts),
        attempt_metrics=tuple(state.attempt_metrics),
        model_usage=tuple(state.model_usage),
        run_metrics=metrics,
        snapshot=snapshot,
        assembly_outcome=assembly_outcome,
    )
    metadata["cost_summary"] = cost_summary
    # Phase 8.3: per-obligation soft-budget hints, with realised borrowing
    # joined from the cost summary above (single source of truth for direct
    # spend). Both projections iterate ``graph.active()``, so every hint has a
    # matching cost entry; unworked obligations borrow nothing.
    obligation_direct = {
        entry["obligation_id"]: entry["direct_cost"]
        for entry in cost_summary["obligations"]
    }
    hints = build_obligation_budget_hints(
        workspace, budget_snapshot=snapshot
    )
    hints = join_borrowed_costs(hints, obligation_direct)
    metadata["budget_hints"] = tuple(hint.to_dict() for hint in hints)
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
