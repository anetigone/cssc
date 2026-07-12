"""Opt-in action-level cost runtime for :class:`StructuredController`."""

from __future__ import annotations

import time
from dataclasses import replace

from agent.proof_system.base import ProofTask
from agent.proof_system.workspace import BranchStatus, SearchActionKind
from agent.search.controller.context import maybe_retrieve
from agent.search.controller.types import ControllerResult
from agent.search.action import ActionGenerationError
from agent.search.execution import ExecutionMode

from ..action_frontier import (
    ActionFrontier,
    ActionFrontierPolicy,
    ProposalCache,
)
from ..branch_ops import branch_by_id
from ..budget_snapshot import BudgetAdmission, admit_estimate
from ..frontier_signals import (
    branch_goal_fingerprints,
    dependents_count,
    is_ready,
    stalled_streak,
)
from ..cost_estimator import (
    ActionCostEstimator,
    CostBucket,
    actual_cost_from_events,
    cost_history_snapshot_from_dict,
    estimate_error,
)
from ..action_runtime_config import ActionCostSource
from ..proposal import StructuredActionProposal
from ..run_state import _StructuredRunState, build_structured_result
from ..model_router import (
    ModelTier,
    RoutingContext,
    route_model,
    routing_metadata,
)
from .action_runtime_ledger import record_proposal_request, unified_budget_snapshot
from .action_runtime_execution import execute_action_node
from .action_runtime_selection import (
    action_generator_uses_model,
    budgeted_priority_explanation,
    proposal_cost_for_route,
    refresh_action_cache,
    select_admissible_action,
)


class StructuredControllerActionRuntimeMixin:
    """Execute cached proposals as globally competing action nodes."""

    def _run_action_runtime(self, task: ProofTask) -> ControllerResult:
        workspace = self._initial_workspace(task)
        state = _StructuredRunState()
        estimator = self.cost_estimator
        serialized_history = task.metadata.get("cost_history_snapshot")
        source = self.action_runtime_config.cost_source
        if source is ActionCostSource.STATIC:
            estimator = None
        elif estimator is None and isinstance(serialized_history, dict):
            estimator = ActionCostEstimator(
                cost_history_snapshot_from_dict(serialized_history)
            )
        if source is ActionCostSource.EMPIRICAL and estimator is None:
            raise ValueError(
                "empirical action cost requires a frozen cost history snapshot"
            )
        self._action_runtime_estimator = estimator
        cache = ProposalCache()
        frontier = ActionFrontier(policy=ActionFrontierPolicy.COST_AWARE_V1)

        while True:
            cache = refresh_action_cache(frontier, workspace, cache, state)
            if not frontier.has_work():
                workspace, cache = self._fill_action_cache(task, workspace, cache, state)
                cache = refresh_action_cache(frontier, workspace, cache, state)
            if not frontier.has_work():
                if state.stop_reason == "budget":
                    reason = self.budget.exhausted_reason()
                    state.stop_reason = f"budget:{reason}" if reason else "no_ready_work"
                break

            snapshot = unified_budget_snapshot(self.budget, state)
            selection = select_admissible_action(
                frontier,
                snapshot,
                enforce_remaining_budget=(
                    self.action_runtime_config.remaining_budget_policy
                ),
            )
            state.proposal_cache_events.append({
                "event": "choice_set",
                "workspace_version": workspace.version,
                "choices": selection.choice_set,
                "selected_node_id": (
                    selection.node.node_id if selection.node is not None else None
                ),
                "budget_snapshot": snapshot.to_dict(),
            })
            if selection.node is None:
                state.stop_reason = "budget:action"
                break
            node = frontier.consume(selection.node.node_id)
            cache = cache.remove(node.node_id)
            branch = branch_by_id(workspace, node.branch_id)
            if branch is None or branch.status is not BranchStatus.ACTIVE:
                continue

            admission = selection.admission
            assert admission is not None
            state.priority_explanations.append(
                budgeted_priority_explanation(selection, snapshot)
            )
            state.proposal_cache_events.append({
                "event": "action_selected" if admission.allowed else "budget_rejected",
                "node_id": node.node_id,
                "branch_id": node.branch_id,
                "action_kind": node.proposal.action.kind.value,
                "model_tier": node.proposal_model_tier,
                "routed_model": node.proposal.metadata.get("routed_model"),
                "routing": node.proposal.metadata.get("routing"),
                "workspace_version": workspace.version,
                "budget_admission": admission.to_dict(),
                "estimated_execution_cost": node.estimated_execution_cost.to_dict(),
            })
            event_start = len(state.cost_ledger.events)
            workspace, terminal, execution_end = execute_action_node(
                self, task,
                workspace,
                branch,
                replace(
                    node.proposal,
                    metadata={
                        **node.proposal.metadata,
                        "proposal_batch_id": node.proposal_batch_id,
                    },
                ),
                node.node_id,
                state,
            )
            actual = actual_cost_from_events(
                state.cost_ledger.events[event_start:execution_end]
            )
            state.proposal_cache_events.append({
                "event": "action_cost_observed",
                "node_id": node.node_id,
                "estimated_execution_cost": node.estimated_execution_cost.to_dict(),
                "actual_execution_cost": actual.to_dict(),
                "estimate_error": estimate_error(
                    node.estimated_execution_cost, actual
                ),
                "ledger_event_ids": tuple(
                    event.event_id
                    for event in state.cost_ledger.events[event_start:execution_end]
                ),
            })
            if terminal is not None:
                terminal.metadata["action_runtime_config"] = (
                    self.action_runtime_config.to_dict()
                )
                terminal.metadata["proposal_cache_events"] = tuple(
                    state.proposal_cache_events
                )
                return terminal
            if state.stop_reason not in {"budget", ""}:
                break

        if state.stop_reason == "budget":
            reason = self.budget.exhausted_reason()
            state.stop_reason = f"budget:{reason}" if reason else "no_ready_work"
        result = build_structured_result(
            state, task, workspace, accepted=False, stop_reason=state.stop_reason,
            execution_mode=ExecutionMode.STRUCTURED, budget=self.budget,
            safety_reviewer=self.safety_reviewer,
            frontier_policy=ActionFrontierPolicy.COST_AWARE_V1.value,
        )
        result.metadata["action_runtime_config"] = self.action_runtime_config.to_dict()
        return result

    def _fill_action_cache(self, task, workspace, cache, state):
        ready = sorted(
            (
                branch for branch in workspace.branches
                if is_ready(branch, workspace)
                and not any(node.branch_id == branch.branch_id for node in cache.entries)
            ),
            key=lambda branch: branch.branch_id,
        )
        for branch in ready:
            uses_model = action_generator_uses_model(self.action_generator)
            if uses_model and not self.budget.can_call_model():
                break
            state.retrieved_this_iteration = False
            state.current_retrieved = maybe_retrieve(
                task, state, self.retriever, self.config,
                is_first_iteration=not state.attempts,
            )
            if state.current_retrieved:
                state.retrieved_history.extend(state.current_retrieved)
            route_decision = self._route_proposal_generation(
                branch, workspace, cache, state
            )
            if uses_model:
                proposal_snapshot = unified_budget_snapshot(self.budget, state)
                proposal_cost = proposal_cost_for_route(
                    route_decision, self.model_router_config
                )
                proposal_admission = (
                    admit_estimate(
                        proposal_snapshot, proposal_cost, reject_unknown=True
                    )
                    if self.action_runtime_config.remaining_budget_policy
                    else BudgetAdmission(
                        True, (), ("remaining_budget_policy_disabled",)
                    )
                )
                state.proposal_cache_events.append({
                    "event": (
                        "proposal_budget_admitted"
                        if proposal_admission.allowed
                        else "proposal_budget_rejected"
                    ),
                    "branch_id": branch.branch_id,
                    "workspace_version": workspace.version,
                    "model_tier": route_decision.tier.value,
                    "estimated_proposal_cost": proposal_cost.to_dict(),
                    "budget_admission": proposal_admission.to_dict(),
                    "budget_snapshot": proposal_snapshot.to_dict(),
                })
                if not proposal_admission.allowed:
                    state.stop_reason = "budget:proposal"
                    return workspace, cache
            if uses_model:
                self.budget.reserve_model_call()
            self._action_route_decision = route_decision
            started = time.perf_counter()
            request_id = f"proposal:{state.sample_id}:{len(state.model_usage)}"
            try:
                proposals = self._generate(task, branch, workspace, state)
            except ActionGenerationError as exc:
                record_proposal_request(
                    state, request_id, branch, time.perf_counter() - started,
                    status="failed", proposals=(), error=type(exc).__name__,
                    provider_used=uses_model, route_decision=route_decision,
                    failure_metadata=exc.metadata,
                )
                state.generation_failures.append({
                    "attempt_index": state.attempt_index,
                    "branch_id": branch.branch_id,
                    "reason": exc.reason,
                    "message": str(exc),
                    **exc.metadata,
                })
                state.stop_reason = f"generation:{exc.reason}"
                self._action_route_decision = None
                return workspace, cache
            finally:
                self._action_route_decision = None
            elapsed = time.perf_counter() - started
            finalized: list[StructuredActionProposal] = []
            for proposal in proposals:
                proposal = self._finalize_kind(proposal, branch)
                ok, errors = proposal.validate()
                if ok:
                    finalized.append(proposal)
                else:
                    state.skipped_proposals.append({
                        "attempt_index": state.attempt_index,
                        "branch_id": branch.branch_id,
                        "kind": proposal.action.kind.value,
                        "errors": errors,
                    })
            source, tier = record_proposal_request(
                state, request_id, branch, elapsed,
                status="completed", proposals=tuple(finalized),
                provider_used=uses_model, route_decision=route_decision,
            )
            cache, reasons = cache.add(
                workspace, finalized, proposal_source=source,
                proposal_batch_id=request_id if source == "model" else None,
                proposal_model_tier=tier,
            )
            cache = self._apply_action_estimates(
                task, workspace, branch, cache, state
            )
            state.proposal_cache_events.append({
                "event": "cache_fill", "branch_id": branch.branch_id,
                "workspace_version": workspace.version,
                "proposal_count": len(finalized), "reasons": reasons,
                "proposal_source": source, "proposal_batch_id": request_id,
            })
            if not finalized:
                from ..branch_ops import block_branch
                workspace = block_branch(workspace, branch.branch_id)
                cache = ProposalCache(cache.valid_nodes(workspace), cache.limits)
        return workspace, cache

    def _route_proposal_generation(self, branch, workspace, cache, state):
        selected = self._select_test_action(branch)
        action_kind = (
            selected.kind
            if selected is not None
            else SearchActionKind.REPAIR_IMPLEMENTATION
            if branch.last_action is not None
            else SearchActionKind.IMPLEMENT
        )
        fingerprints = branch_goal_fingerprints(branch)
        same_goal_failures = stalled_streak(branch)
        validation_failures = sum(
            1
            for item in state.skipped_proposals
            if item.get("branch_id") == branch.branch_id and item.get("errors")
        )
        snapshot = unified_budget_snapshot(self.budget, state)
        decision = route_model(
            RoutingContext(
                action_kind=action_kind,
                goal_fingerprint=fingerprints[0] if fingerprints else None,
                cheap_failures_on_fingerprint=same_goal_failures,
                proposal_validation_failures=validation_failures,
                stalled_streak=same_goal_failures,
                unlock_value=dependents_count(
                    workspace.obligation_graph
                ).get(branch.obligation_id, 0),
                has_trusted_cheap_cached_action=any(
                    node.branch_id == branch.branch_id
                    and node.proposal_model_tier == ModelTier.CHEAP.value
                    for node in cache.entries
                ),
                is_low_cost_capability_probe=(
                    action_kind is SearchActionKind.RUN_CAPABILITY_TEST
                ),
            ),
            snapshot,
            config=self.model_router_config,
        )
        state.proposal_cache_events.append({
            "event": "model_routed",
            "branch_id": branch.branch_id,
            "workspace_version": workspace.version,
            **routing_metadata(decision),
        })
        return decision

    def _apply_action_estimates(self, task, workspace, branch, cache, state):
        estimator = self._action_runtime_estimator
        if estimator is None:
            return cache
        estimates = {}
        for node in cache.entries:
            if node.branch_id != branch.branch_id:
                continue
            obligation = workspace.obligation_graph.by_id(branch.obligation_id)
            statement_size = len(obligation.lean_statement or "") if obligation else 0
            goal_count = len(branch_goal_fingerprints(branch))
            imports_profile = (
                "none" if not task.imports
                else "mathlib" if any("Mathlib" in item for item in task.imports)
                else "custom"
            )
            bucket = CostBucket(
                model=node.proposal.metadata.get("model"),
                model_tier=node.proposal_model_tier,
                action_kind=node.proposal.action.kind,
                imports_profile=imports_profile,
                goal_size_bucket=("empty" if goal_count == 0 else "small" if goal_count <= 2 else "large"),
                obligation_size_bucket=("small" if statement_size <= 500 else "large"),
                repair_state=("repair" if branch.last_action is not None else "fresh"),
                stalled=stalled_streak(branch) > 1,
            )
            estimation = estimator.estimate(bucket)
            estimates[node.node_id] = estimation.estimate
            state.proposal_cache_events.append({
                "event": "cost_estimated", "node_id": node.node_id,
                **estimation.to_dict(),
            })
        return cache.with_estimates(estimates)
