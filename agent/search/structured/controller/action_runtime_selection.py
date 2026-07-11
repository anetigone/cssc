"""Budget-constrained selection helpers for the Phase 9 action runtime."""

from __future__ import annotations

from dataclasses import dataclass

from ..action_frontier import (
    ActionFrontier,
    ActionFrontierNode,
    CostEstimate,
    ProposalCache,
    node_invalid_reason,
)
from ..budget_snapshot import BudgetAdmission, UnifiedBudgetSnapshot, admit_estimate
from ..model_router import ModelRouterConfig, ModelTier, RouteDecision
from ..frontier_types import PriorityExplanation


@dataclass(frozen=True)
class ConstrainedSelection:
    node: ActionFrontierNode | None
    admission: BudgetAdmission | None
    choice_set: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class BudgetedPriorityExplanation:
    """Action pop explanation enriched with its frozen budget decision."""

    base: PriorityExplanation
    selected_budget_admission: dict[str, object]
    rejected_alternatives: tuple[dict[str, object], ...]
    global_reserve_checks: int
    global_reserve_model_requests: int
    obligation_budget_context: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            **self.base.to_dict(),
            "selected_budget_admission": self.selected_budget_admission,
            "rejected_alternatives": self.rejected_alternatives,
            "global_reserve_checks": self.global_reserve_checks,
            "global_reserve_model_requests": self.global_reserve_model_requests,
            "obligation_budget_context": self.obligation_budget_context,
        }


def select_admissible_action(
    frontier: ActionFrontier,
    snapshot: UnifiedBudgetSnapshot,
) -> ConstrainedSelection:
    """Pick the highest-ranked feasible action from one frozen choice set."""
    selected = None
    selected_admission = None
    rows: list[dict[str, object]] = []
    for node in frontier.ranked():
        admission = admit_estimate(snapshot, node.estimated_execution_cost)
        rows.append({
            "node_id": node.node_id,
            "branch_id": node.branch_id,
            "action_kind": node.proposal.action.kind.value,
            "estimated_execution_cost": node.estimated_execution_cost.to_dict(),
            "budget_admission": admission.to_dict(),
        })
        if selected is None and admission.allowed:
            selected = node
            selected_admission = admission
    return ConstrainedSelection(selected, selected_admission, tuple(rows))


def budgeted_priority_explanation(
    selection: ConstrainedSelection,
    snapshot: UnifiedBudgetSnapshot,
) -> BudgetedPriorityExplanation:
    if selection.node is None or selection.admission is None:
        raise ValueError("budgeted priority explanation requires a selected node")
    obligation = snapshot.obligations.get(selection.node.obligation_id)
    return BudgetedPriorityExplanation(
        base=selection.node.priority_explanation,
        selected_budget_admission=selection.admission.to_dict(),
        rejected_alternatives=tuple(
            row for row in selection.choice_set
            if not row["budget_admission"]["allowed"]  # type: ignore[index]
        ),
        global_reserve_checks=snapshot.global_reserve_checks,
        global_reserve_model_requests=snapshot.global_reserve_model_requests,
        obligation_budget_context=(obligation.to_dict() if obligation else None),
    )


def proposal_cost_for_route(
    decision: RouteDecision,
    config: ModelRouterConfig,
) -> CostEstimate:
    """Return the frozen generation prior for the tier selected this round."""
    return (
        config.strong_cost
        if decision.tier is ModelTier.STRONG
        else config.cheap_cost
    )


def action_generator_uses_model(generator) -> bool:
    """Distinguish controlled proposal sources before reserving budget."""
    if getattr(generator, "_uses_model", False):
        return True
    legacy = getattr(generator, "_legacy", None)
    candidate = legacy if legacy is not None else generator
    return hasattr(candidate, "config") and hasattr(candidate, "transport")


def refresh_action_cache(frontier, workspace, cache, state) -> ProposalCache:
    """Refresh the frontier and trace every deterministically stale node."""
    for node in cache.entries:
        reason = node_invalid_reason(node, workspace)
        if reason is not None:
            state.proposal_cache_events.append({
                "event": "cache_invalidated",
                "node_id": node.node_id,
                "branch_id": node.branch_id,
                "action_kind": node.proposal.action.kind.value,
                "cached_at_workspace_version": node.cached_at_workspace_version,
                "workspace_version": workspace.version,
                "reason": reason,
            })
    return frontier.refresh(workspace, cache)
