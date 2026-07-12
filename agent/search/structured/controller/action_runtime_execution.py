"""Execution of one selected action-frontier node."""

from __future__ import annotations

from dataclasses import replace

from agent.proof_system.base import DiagnosticCategory
from agent.proof_system.workspace import SearchActionKind
from agent.search.controller.types import AttemptRecord
from agent.search.execution import ExecutionMode
from agent.search.metrics import attempt_metric

from ..action_frontier import ActionFrontierPolicy
from ..branch_ops import edit_with_structured_metadata, expand_candidate_branches
from ..finalize import assemble_and_finalize
from ..reducer import StructuredActionResult, apply
from ..solution_tracker import has_complete_solution
from .action_runtime_ledger import attribute_proposal_batch


def execute_action_node(controller, task, workspace, branch, proposal, node_id, state):
    """Execute one action and return the pre-assembly ledger boundary."""
    kind = proposal.action.kind
    proposal = replace(
        proposal, metadata={**proposal.metadata, "action_node_id": node_id}
    )
    attribute_proposal_batch(state, proposal, node_id)
    if kind is SearchActionKind.RUN_CAPABILITY_TEST:
        workspace, _ = controller._run_capability_audits(
            task, branch, [proposal], workspace, state
        )
        return workspace, None, len(state.cost_ledger.events)
    if kind is SearchActionKind.DECOMPOSE:
        workspace, _ = controller._run_decompose(
            task, branch, [proposal], workspace, state
        )
        return workspace, None, len(state.cost_ledger.events)
    if kind in {SearchActionKind.PROPOSE_ARGUMENT, SearchActionKind.REFINE_ARGUMENT}:
        workspace, _ = controller._run_argument(
            task, branch, [proposal], workspace, state
        )
        return workspace, None, len(state.cost_ledger.events)
    if kind is SearchActionKind.CHANGE_REPRESENTATION:
        workspace, _ = controller._run_change_representation(
            task, branch, [proposal], workspace, state
        )
        return workspace, None, len(state.cost_ledger.events)
    if kind not in {SearchActionKind.IMPLEMENT, SearchActionKind.REPAIR_IMPLEMENTATION}:
        return workspace, None, len(state.cost_ledger.events)

    workspace, candidates = expand_candidate_branches(
        workspace, branch, 1, state.attempt_index
    )
    if not candidates:
        return workspace, None, len(state.cost_ledger.events)
    candidate = candidates[0]
    proposal = controller._finalize_kind(proposal, candidate)
    proof_text = proposal.payload.proof_text
    check_task, artifact_source = controller._render_target(
        task, workspace, candidate, proof_text
    )
    edit = edit_with_structured_metadata(
        controller._proposal_edit(proposal, proof_text, branch),
        proposal.action,
        candidate,
    )
    check_result = controller._check(check_task, edit, state)
    safety = controller._review(check_task, edit, check_result, state)
    record = AttemptRecord(
        attempt_index=state.attempt_index,
        candidate_id=edit.action,
        edit=edit,
        candidate_file=check_result.candidate_file,
        check_result=check_result,
    )
    state.attempts.append(record)
    state.attempt_metrics.append(attempt_metric(
        state.attempt_index, action=edit.action, check_result=check_result
    ))
    if check_result.parsed_feedback is not None:
        state.feedback_history.append(check_result.parsed_feedback)
    state.attempt_index += 1
    workspace = apply(workspace, proposal.action, StructuredActionResult(
        branch_id=candidate.branch_id,
        check_result=check_result,
        safety_verdict=safety,
        proof_text=proof_text,
        source=artifact_source,
        attempt_index=record.attempt_index,
    ))
    if (
        controller.config.stop_on_tool_unavailable
        and check_result.category is DiagnosticCategory.TOOL_UNAVAILABLE
    ):
        state.stop_reason = "tool_unavailable"
    execution_end = len(state.cost_ledger.events)
    if has_complete_solution(workspace):
        terminal = assemble_and_finalize(
            task,
            workspace,
            state,
            budget=controller.budget,
            adapter=controller.adapter,
            assembler=controller.assembler,
            check_workspace=controller.check_workspace,
            safety_reviewer=controller.safety_reviewer,
            execution_mode=ExecutionMode.STRUCTURED,
            frontier_policy=ActionFrontierPolicy.COST_AWARE_V1.value,
        )
        return workspace, terminal, execution_end
    return workspace, None, execution_end
