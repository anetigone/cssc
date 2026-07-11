"""Opt-in action-level cost runtime for :class:`StructuredController`."""

from __future__ import annotations

import time
from dataclasses import replace

from agent.proof_system.base import DiagnosticCategory, ProofTask
from agent.proof_system.workspace import BranchStatus, SearchActionKind
from agent.search.controller.context import maybe_retrieve
from agent.search.controller.types import AttemptRecord, ControllerResult
from agent.search.action import ActionGenerationError
from agent.search.execution import ExecutionMode
from agent.search.metrics import attempt_metric

from ..action_frontier import (
    ActionFrontier,
    ActionFrontierPolicy,
    ProposalCache,
)
from ..branch_ops import branch_by_id, edit_with_structured_metadata, expand_candidate_branches
from ..budget_snapshot import admit_estimate
from ..finalize import assemble_and_finalize
from ..frontier_signals import (
    branch_goal_fingerprints,
    dependents_count,
    is_ready,
    stalled_streak,
)
from ..cost_estimator import (
    ActionCostEstimator,
    CostBucket,
    cost_history_snapshot_from_dict,
)
from ..proposal import StructuredActionProposal
from ..reducer import StructuredActionResult, apply
from ..run_state import _StructuredRunState, build_structured_result
from ..solution_tracker import has_complete_solution
from ..model_router import (
    ModelTier,
    RoutingContext,
    route_model,
    routing_metadata,
)
from .action_runtime_ledger import (
    attribute_proposal_batch,
    record_proposal_request,
    unified_budget_snapshot,
)


class StructuredControllerActionRuntimeMixin:
    """Execute cached proposals as globally competing action nodes."""

    def _run_action_runtime(self, task: ProofTask) -> ControllerResult:
        workspace = self._initial_workspace(task)
        state = _StructuredRunState()
        estimator = self.cost_estimator
        serialized_history = task.metadata.get("cost_history_snapshot")
        if estimator is None and isinstance(serialized_history, dict):
            estimator = ActionCostEstimator(
                cost_history_snapshot_from_dict(serialized_history)
            )
        self._action_runtime_estimator = estimator
        cache = ProposalCache()
        frontier = ActionFrontier(policy=ActionFrontierPolicy.COST_AWARE_V1)

        while True:
            cache = frontier.refresh(workspace, cache)
            if not frontier.has_work():
                workspace, cache = self._fill_action_cache(task, workspace, cache, state)
                cache = frontier.refresh(workspace, cache)
            if not frontier.has_work():
                if state.stop_reason == "budget":
                    reason = self.budget.exhausted_reason()
                    state.stop_reason = f"budget:{reason}" if reason else "no_ready_work"
                break

            node = frontier.pop()
            cache = cache.remove(node.node_id)
            branch = branch_by_id(workspace, node.branch_id)
            if branch is None or branch.status is not BranchStatus.ACTIVE:
                continue

            snapshot = unified_budget_snapshot(self.budget, state)
            admission = admit_estimate(snapshot, node.estimated_execution_cost)
            state.priority_explanations.append(node.priority_explanation)
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
            if not admission.allowed:
                state.stop_reason = "budget:action"
                break

            workspace, terminal = self._execute_action_node(
                task,
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
            if terminal is not None:
                return terminal
            if state.stop_reason not in {"budget", ""}:
                break

        if state.stop_reason == "budget":
            reason = self.budget.exhausted_reason()
            state.stop_reason = f"budget:{reason}" if reason else "no_ready_work"
        return build_structured_result(
            state, task, workspace, accepted=False, stop_reason=state.stop_reason,
            execution_mode=ExecutionMode.STRUCTURED, budget=self.budget,
            safety_reviewer=self.safety_reviewer,
            frontier_policy=ActionFrontierPolicy.COST_AWARE_V1.value,
        )

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
            uses_model = self._action_generator_uses_model()
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

    def _action_generator_uses_model(self) -> bool:
        """Distinguish controlled/deterministic proposal sources before spending."""
        generator = self.action_generator
        if getattr(generator, "_uses_model", False):
            return True
        legacy = getattr(generator, "_legacy", None)
        candidate = legacy if legacy is not None else generator
        return hasattr(candidate, "config") and hasattr(candidate, "transport")

    def _execute_action_node(self, task, workspace, branch, proposal, node_id, state):
        kind = proposal.action.kind
        proposal = replace(proposal, metadata={**proposal.metadata, "action_node_id": node_id})
        attribute_proposal_batch(state, proposal, node_id)
        if kind is SearchActionKind.RUN_CAPABILITY_TEST:
            workspace, _ = self._run_capability_audits(task, branch, [proposal], workspace, state)
            return workspace, None
        if kind is SearchActionKind.DECOMPOSE:
            workspace, _ = self._run_decompose(task, branch, [proposal], workspace, state)
            return workspace, None
        if kind in {SearchActionKind.PROPOSE_ARGUMENT, SearchActionKind.REFINE_ARGUMENT}:
            workspace, _ = self._run_argument(task, branch, [proposal], workspace, state)
            return workspace, None
        if kind is SearchActionKind.CHANGE_REPRESENTATION:
            workspace, _ = self._run_change_representation(task, branch, [proposal], workspace, state)
            return workspace, None
        if kind not in {SearchActionKind.IMPLEMENT, SearchActionKind.REPAIR_IMPLEMENTATION}:
            return workspace, None

        workspace, candidates = expand_candidate_branches(
            workspace, branch, 1, state.attempt_index
        )
        if not candidates:
            return workspace, None
        candidate = candidates[0]
        proposal = self._finalize_kind(proposal, candidate)
        proof_text = proposal.payload.proof_text
        check_task, artifact_source = self._render_target(task, workspace, candidate, proof_text)
        edit = edit_with_structured_metadata(
            self._proposal_edit(proposal, proof_text, branch), proposal.action, candidate,
        )
        check_result = self._check(check_task, edit, state)
        safety = self._review(check_task, edit, check_result, state)
        record = AttemptRecord(
            attempt_index=state.attempt_index, candidate_id=edit.action, edit=edit,
            candidate_file=check_result.candidate_file, check_result=check_result,
        )
        state.attempts.append(record)
        state.attempt_metrics.append(attempt_metric(
            state.attempt_index, action=edit.action, check_result=check_result,
        ))
        if check_result.parsed_feedback is not None:
            state.feedback_history.append(check_result.parsed_feedback)
        state.attempt_index += 1
        workspace = apply(workspace, proposal.action, StructuredActionResult(
            branch_id=candidate.branch_id, check_result=check_result,
            safety_verdict=safety, proof_text=proof_text, source=artifact_source,
            attempt_index=record.attempt_index,
        ))
        if self.config.stop_on_tool_unavailable and check_result.category is DiagnosticCategory.TOOL_UNAVAILABLE:
            state.stop_reason = "tool_unavailable"
        if has_complete_solution(workspace):
            return workspace, assemble_and_finalize(
                task, workspace, state, budget=self.budget, adapter=self.adapter,
                assembler=self.assembler, check_workspace=self.check_workspace,
                safety_reviewer=self.safety_reviewer,
                execution_mode=ExecutionMode.STRUCTURED,
                frontier_policy=ActionFrontierPolicy.COST_AWARE_V1.value,
            )
        return workspace, None
