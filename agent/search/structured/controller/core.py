"""Structured execution mode controller."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from agent.proof_system.assembler import ArtifactAssembler
from agent.proof_system.base import (
    CandidateEdit,
    DiagnosticCategory,
    ProofSystemAdapter,
    ProofTask,
)
from agent.proof_system.workspace import (
    DEFAULT_ALLOWED_MUTATIONS,
    BranchStatus,
    ObligationStatus,
    ProofBranch,
    ProofWorkspace,
    SearchAction,
    SearchActionKind,
    WorkspaceStatus,
    initialize_from_task,
)
from agent.search.action import (
    ActionGenerationError,
    ActionGenerationRequest,
    ActionGenerator,
)
from agent.search.budget import BudgetConfig, BudgetManager
from agent.search.controller.types import (
    AttemptRecord,
    ControllerConfig,
    ControllerResult,
    Retriever,
)
from agent.search.controller.context import maybe_retrieve, summarize_context
from agent.search.execution import ExecutionMode
from agent.search.metrics import attempt_metric
from agent.search.safety import SafetyReviewer, SafetyVerdict, StatementSafetyReviewer
from agent.agents.context import ContextSummarizer
from agent.runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace
from ..branch_ops import (
    block_branch,
    branch_by_id,
    edit_with_structured_metadata,
    expand_candidate_branches,
)
from .actions import StructuredControllerActionMixin
from .runtime import StructuredControllerRuntimeMixin
from ..finalize import assemble_and_finalize
from ..frontier import Frontier
from ..proposal import (
    StructuredActionProposal,
    adapt_legacy_generator,
)
from ..reducer import (
    StructuredActionResult,
    apply,
)
from ..run_state import _StructuredRunState, build_structured_result
from ..solution_tracker import has_complete_solution

logger = logging.getLogger(__name__)


class StructuredController(
    StructuredControllerActionMixin,
    StructuredControllerRuntimeMixin,
):
    """Coordinate frontier scheduling, proposal execution, and final assembly."""

    def __init__(
        self,
        *,
        adapter: ProofSystemAdapter,
        action_generator: ActionGenerator,
        workspace: AttemptWorkspace,
        check_workspace: EphemeralCheckWorkspace | None = None,
        retriever: Retriever | None = None,
        context_summarizer: ContextSummarizer | None = None,
        budget_config: BudgetConfig | None = None,
        config: ControllerConfig | None = None,
        safety_reviewer: SafetyReviewer | None = None,
    ) -> None:
        self.adapter = adapter
        self.action_generator = adapt_legacy_generator(action_generator)
        self.workspace = workspace
        self.check_workspace = check_workspace
        self.retriever = retriever
        self.context_summarizer = context_summarizer
        self.budget = BudgetManager(budget_config)
        self.config = config or ControllerConfig()
        if self.config.execution_mode != ExecutionMode.STRUCTURED:
            raise ValueError(
                "StructuredController requires execution_mode=STRUCTURED, "
                f"got {self.config.execution_mode!s}."
            )
        self.safety_reviewer = safety_reviewer or StatementSafetyReviewer()
        self.assembler = ArtifactAssembler()

    def run(self, task: ProofTask) -> ControllerResult:
        logger.info("Structured controller run started: task_id=%s", task.task_id)
        workspace = self._initial_workspace(task)
        state = _StructuredRunState()
        frontier = Frontier()
        frontier.seed(workspace)
        logger.debug(
            "Structured frontier seeded: task_id=%s has_work=%s checks_available=%s "
            "model_calls_available=%s",
            task.task_id,
            frontier.has_work(),
            self.budget.can_check(),
            self.budget.can_call_model(),
        )

        while (
            frontier.has_work()
            and self.budget.can_check()
            and self.budget.can_call_model()
        ):
            node = frontier.pop()
            branch = branch_by_id(workspace, node.branch_id)
            if branch is None or branch.status != BranchStatus.ACTIVE:
                logger.debug(
                    "Structured frontier node skipped: task_id=%s branch=%s reason=%s",
                    task.task_id,
                    node.branch_id,
                    "missing" if branch is None else branch.status.value,
                )
                continue
            logger.info(
                "Structured frontier pop: task_id=%s branch=%s obligation=%s "
                "attempt_index=%d stalled_streak=%d attempts=%d",
                task.task_id,
                branch.branch_id,
                branch.obligation_id,
                state.attempt_index,
                node.stalled_streak,
                node.attempt_count,
            )

            state.retrieved_this_iteration = False
            state.current_retrieved = maybe_retrieve(
                task,
                state,
                self.retriever,
                self.config,
                is_first_iteration=not state.attempts,
            )
            if state.current_retrieved:
                state.retrieved_history.extend(state.current_retrieved)
                logger.debug(
                    "Structured retrieval attached: task_id=%s branch=%s retrieved=%d history=%d",
                    task.task_id,
                    branch.branch_id,
                    len(state.current_retrieved),
                    len(state.retrieved_history),
                )
            self.budget.reserve_model_call()
            logger.debug(
                "Structured model call reserved: task_id=%s branch=%s model_calls_used=%d "
                "remaining_model_calls=%s",
                task.task_id,
                branch.branch_id,
                self.budget.snapshot().model_calls_used,
                self.budget.snapshot().remaining_model_calls,
            )
            try:
                proposals = self._generate(task, branch, workspace, state)
            except ActionGenerationError as exc:
                failure = {
                    "attempt_index": state.attempt_index,
                    "branch_id": branch.branch_id,
                    "reason": exc.reason,
                    "message": str(exc),
                    **exc.metadata,
                }
                state.generation_failures.append(failure)
                usage = exc.metadata.get("token_usage")
                usage_entry = dict(usage) if isinstance(usage, dict) else {}
                usage_entry.setdefault("input_tokens", 0)
                usage_entry.setdefault("output_tokens", 0)
                # This is a call ledger as well as token diagnostics: reserve_model_call()
                # succeeded above even when the provider reports no token usage.
                usage_entry["structured_branch_id"] = branch.branch_id
                usage_entry["structured_obligation_id"] = branch.obligation_id
                state.model_usage.append(usage_entry)
                state.stop_reason = f"generation:{exc.reason}"
                logger.warning(
                    "Structured generation failed: task_id=%s branch=%s reason=%s",
                    task.task_id,
                    branch.branch_id,
                    state.stop_reason,
                )
                break
            usage = proposals[0].metadata.get("token_usage") if proposals else None
            usage_entry = dict(usage) if isinstance(usage, dict) else {}
            usage_entry.setdefault("input_tokens", 0)
            usage_entry.setdefault("output_tokens", 0)
            # This is a call ledger as well as token diagnostics: reserve_model_call()
            # succeeded above even when the provider reports no token usage.
            usage_entry["structured_branch_id"] = branch.branch_id
            usage_entry["structured_obligation_id"] = branch.obligation_id
            state.model_usage.append(usage_entry)
            if not proposals:
                workspace = block_branch(workspace, branch.branch_id)
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    state.stop_reason = "no_actions"
                logger.info(
                    "Structured branch blocked after empty proposals: task_id=%s branch=%s stop_reason=%s",
                    task.task_id,
                    branch.branch_id,
                    state.stop_reason,
                )
                continue

            workspace = self._fold_failure_hypotheses(
                branch, list(proposals), workspace
            )
            branch = branch_by_id(workspace, branch.branch_id) or branch
            proposals = self._prioritize_selected_test(branch, tuple(proposals))

            executable_proposals: list[StructuredActionProposal] = []
            capability_proposals: list[StructuredActionProposal] = []
            decompose_proposals: list[StructuredActionProposal] = []
            argument_proposals: list[StructuredActionProposal] = []
            representation_proposals: list[StructuredActionProposal] = []
            for proposal in proposals:
                proposal = self._finalize_kind(proposal, branch)
                ok, errors = proposal.validate()
                if not ok:
                    logger.warning(
                        "Structured proposal invalid, skipping: %s", errors
                    )
                    continue
                action = proposal.action
                if action.kind is SearchActionKind.RUN_CAPABILITY_TEST:
                    capability_proposals.append(proposal)
                    continue
                if action.kind is SearchActionKind.DECOMPOSE:
                    decompose_proposals.append(proposal)
                    continue
                if action.kind in (
                    SearchActionKind.PROPOSE_ARGUMENT,
                    SearchActionKind.REFINE_ARGUMENT,
                ):
                    argument_proposals.append(proposal)
                    continue
                if action.kind is SearchActionKind.CHANGE_REPRESENTATION:
                    representation_proposals.append(proposal)
                    continue
                if action.kind not in (
                    SearchActionKind.IMPLEMENT,
                    SearchActionKind.REPAIR_IMPLEMENTATION,
                ):
                    state.skipped_proposals.append(
                        {
                            "attempt_index": state.attempt_index,
                            "kind": action.kind.value,
                            "rationale": action.rationale,
                        }
                    )
                    continue
                executable_proposals.append(proposal)

            logger.debug(
                "Structured proposals classified: task_id=%s branch=%s implement=%d "
                "capability=%d decompose=%d argument=%d representation=%d skipped=%d",
                task.task_id,
                branch.branch_id,
                len(executable_proposals),
                len(capability_proposals),
                len(decompose_proposals),
                len(argument_proposals),
                len(representation_proposals),
                len(state.skipped_proposals),
            )

            if capability_proposals:
                logger.info(
                    "Structured capability audits starting: task_id=%s branch=%s count=%d",
                    task.task_id,
                    branch.branch_id,
                    len(capability_proposals),
                )
                workspace, stop_for_capability = self._run_capability_audits(
                    task, branch, capability_proposals, workspace, state
                )
                if stop_for_capability or state.stop_reason != "budget":
                    frontier.update(workspace, branch.branch_id, accepted=False)
                    if not frontier.has_work():
                        if not state.stop_reason:
                            state.stop_reason = "no_actions"
                    logger.info(
                        "Structured capability audits stopped branch: task_id=%s branch=%s stop_reason=%s frontier_has_work=%s",
                        task.task_id,
                        branch.branch_id,
                        state.stop_reason,
                        frontier.has_work(),
                    )
                    continue

            if decompose_proposals:
                workspace, _ = self._run_decompose(
                    task, branch, decompose_proposals, workspace, state
                )
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    if not state.stop_reason:
                        state.stop_reason = "no_actions"
                logger.debug(
                    "Structured frontier refreshed after decompose: task_id=%s branch=%s has_work=%s stop_reason=%s",
                    task.task_id,
                    branch.branch_id,
                    frontier.has_work(),
                    state.stop_reason,
                )
                continue

            if argument_proposals:
                workspace, _ = self._run_argument(
                    task, branch, argument_proposals, workspace, state
                )
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    if not state.stop_reason:
                        state.stop_reason = "no_actions"
                logger.debug(
                    "Structured frontier refreshed after argument edit: task_id=%s branch=%s has_work=%s stop_reason=%s",
                    task.task_id,
                    branch.branch_id,
                    frontier.has_work(),
                    state.stop_reason,
                )
                continue

            if representation_proposals:
                workspace, _ = self._run_change_representation(
                    task, branch, representation_proposals, workspace, state
                )
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    if not state.stop_reason:
                        state.stop_reason = "no_actions"
                logger.debug(
                    "Structured frontier refreshed after representation change: task_id=%s branch=%s has_work=%s stop_reason=%s",
                    task.task_id,
                    branch.branch_id,
                    frontier.has_work(),
                    state.stop_reason,
                )
                continue

            executable_proposals = executable_proposals[
                : self.config.max_candidates_per_model_call
            ]
            logger.debug(
                "Structured implement proposals selected: task_id=%s branch=%s count=%d max=%d",
                task.task_id,
                branch.branch_id,
                len(executable_proposals),
                self.config.max_candidates_per_model_call,
            )

            workspace, candidate_branches = expand_candidate_branches(
                workspace,
                branch,
                len(executable_proposals),
                state.attempt_index,
            )
            stop_for_tool = False
            attempted_branch_ids: list[str] = []
            for proposal, candidate_branch in zip(executable_proposals, candidate_branches):
                if not self.budget.can_check():
                    state.stop_reason = "budget:checks"
                    logger.info(
                        "Structured check budget exhausted before candidate: task_id=%s branch=%s",
                        task.task_id,
                        candidate_branch.branch_id,
                    )
                    break
                proposal = self._finalize_kind(proposal, candidate_branch)
                action = proposal.action
                proof_text = proposal.payload.proof_text  # type: ignore[union-attr]
                logger.debug(
                    "Structured implementation attempt starting: task_id=%s branch=%s kind=%s proof_chars=%d",
                    task.task_id,
                    candidate_branch.branch_id,
                    action.kind.value,
                    len(proof_text),
                )
                check_task, artifact_source = self._render_target(
                    task, workspace, candidate_branch, proof_text
                )
                edit = edit_with_structured_metadata(
                    self._proposal_edit(proposal, proof_text, branch),
                    action,
                    candidate_branch,
                )
                check_result = self._check(check_task, edit, state)
                attempted_branch_ids.append(candidate_branch.branch_id)
                safety_verdict = self._review(check_task, edit, check_result, state)
                record = AttemptRecord(
                    attempt_index=state.attempt_index,
                    candidate_id=edit.action,
                    edit=edit,
                    candidate_file=check_result.candidate_file,
                    check_result=check_result,
                )
                state.attempts.append(record)
                state.attempt_metrics.append(
                    attempt_metric(
                        state.attempt_index,
                        action=edit.action,
                        check_result=check_result,
                    )
                )
                if check_result.parsed_feedback is not None:
                    state.feedback_history.append(check_result.parsed_feedback)
                state.attempt_index += 1
                workspace = apply(
                    workspace,
                    action,
                    StructuredActionResult(
                        branch_id=candidate_branch.branch_id,
                        check_result=check_result,
                        safety_verdict=safety_verdict,
                        proof_text=proof_text,
                        source=artifact_source,
                        attempt_index=record.attempt_index,
                    ),
                )
                logger.info(
                    "Structured attempt checked: task_id=%s attempt_index=%d accepted=%s category=%s",
                    task.task_id,
                    record.attempt_index,
                    check_result.accepted,
                    check_result.category.value,
                )
                if (
                    self.config.stop_on_tool_unavailable
                    and check_result.category == DiagnosticCategory.TOOL_UNAVAILABLE
                ):
                    state.stop_reason = "tool_unavailable"
                    stop_for_tool = True
                    break
                if has_complete_solution(workspace):
                    return assemble_and_finalize(
                        task,
                        workspace,
                        state,
                        budget=self.budget,
                        adapter=self.adapter,
                        assembler=self.assembler,
                        check_workspace=self.check_workspace,
                        safety_reviewer=self.safety_reviewer,
                        execution_mode=ExecutionMode.STRUCTURED,
                    )

            frontier.update(
                workspace,
                branch.branch_id,
                accepted=False,
                attempted_branch_ids=tuple(attempted_branch_ids),
            )
            logger.debug(
                "Structured frontier refreshed after attempts: task_id=%s branch=%s "
                "attempted=%s has_work=%s stop_reason=%s",
                task.task_id,
                branch.branch_id,
                attempted_branch_ids,
                frontier.has_work(),
                state.stop_reason,
            )
            if stop_for_tool or state.stop_reason == "budget:checks":
                break

        if state.stop_reason == "budget":
            reason = self.budget.exhausted_reason()
            if reason is not None:
                state.stop_reason = f"budget:{reason}"
                logger.info(
                    "Structured budget exhausted: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
        if state.stop_reason == "budget" and not frontier.has_work():
            state.stop_reason = "no_ready_work"
            logger.info(
                "Structured run has no ready work: task_id=%s workspace_version=%d",
                task.task_id,
                workspace.version,
            )
        if state.stop_reason == "no_ready_work" and self._has_blocked_obligation(
            workspace
        ):
            state.stop_reason = "blocked"
            logger.info(
                "Structured run terminal reason sharpened to blocked: task_id=%s",
                task.task_id,
            )
        result = build_structured_result(
            state,
            task,
            workspace,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=ExecutionMode.STRUCTURED,
            budget=self.budget,
            safety_reviewer=self.safety_reviewer,
        )
        logger.info(
            "Structured controller run finished: task_id=%s accepted=%s stop_reason=%s "
            "attempts=%d checks=%d model_calls=%d",
            task.task_id,
            result.accepted,
            result.stop_reason,
            len(result.attempts),
            result.budget.checks_used,
            result.budget.model_calls_used,
        )
        return result
