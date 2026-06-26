"""Structural action executors mixed into :class:`StructuredController`."""

from __future__ import annotations

import logging

from agent.proof_system.base import CandidateEdit, DiagnosticCategory, ProofTask
from agent.proof_system.workspace import (
    BranchStatus,
    ProofBranch,
    ProofWorkspace,
    SearchAction,
    SearchActionKind,
)
from agent.search.controller.types import AttemptRecord
from agent.search.metrics import attempt_metric
from agent.search.safety import SafetyVerdict
from ..branch_ops import branch_by_id
from ..proposal import (
    FAILURE_HYPOTHESES_KEY,
    CapabilityTestPayload,
    ChangeRepresentationPayload,
    DecomposePayload,
    ProposeArgumentPayload,
    RefineArgumentPayload,
    StructuredActionProposal,
)
from ..reducer import (
    StructuredActionResult,
    apply,
    apply_argument,
    apply_change_representation,
    apply_decompose,
    apply_failure_hypotheses,
)
from ..run_state import _StructuredRunState

logger = logging.getLogger(__name__)


class StructuredControllerActionMixin:
    """Capability, decomposition, argument, and hypothesis helper methods."""

    def _run_capability_audits(
        self,
        task: ProofTask,
        branch: ProofBranch,
        proposals: list[StructuredActionProposal],
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[ProofWorkspace, bool]:
        for proposal in proposals:
            if not self.budget.can_check():
                state.stop_reason = "budget:checks"
                return workspace, True
            proposal = self._finalize_kind(proposal, branch)
            payload = proposal.payload
            assert isinstance(payload, CapabilityTestPayload)
            edit = CandidateEdit(
                text=payload.signature,
                action="capability_test",
                parent_node_id=branch.branch_id,
                metadata={
                    "capability_requirement": payload.requirement,
                    "structured_action_kind": proposal.action.kind.value,
                    "structured_branch_id": branch.branch_id,
                },
            )
            check_result = self._check(task, edit, state)
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
            attempt_index = state.attempt_index
            state.attempt_index += 1
            workspace = apply(
                workspace,
                proposal.action,
                StructuredActionResult(
                    branch_id=branch.branch_id,
                    check_result=check_result,
                    safety_verdict=SafetyVerdict(accepted=False),
                    proof_text=payload.signature,
                    source=payload.signature,
                    attempt_index=attempt_index,
                ),
            )
            if (
                self.config.stop_on_tool_unavailable
                and check_result.category == DiagnosticCategory.TOOL_UNAVAILABLE
            ):
                state.stop_reason = "tool_unavailable"
                return workspace, True
            updated = branch_by_id(workspace, branch.branch_id)
            if updated is None or updated.status != BranchStatus.ACTIVE:
                return workspace, True
        return workspace, False

    def _run_decompose(
        self,
        task: ProofTask,
        branch: ProofBranch,
        proposals: list[StructuredActionProposal],
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[ProofWorkspace, bool]:
        for proposal in proposals:
            proposal = self._finalize_kind(proposal, branch)
            ok, errors = proposal.validate()
            if not ok:
                logger.warning("Decompose proposal invalid, skipping: %s", errors)
                continue
            payload = proposal.payload
            assert isinstance(payload, DecomposePayload)
            current = branch_by_id(workspace, branch.branch_id)
            if current is None or current.status != BranchStatus.ACTIVE:
                _record_skipped(state, proposal)
                continue
            workspace = apply_decompose(
                workspace,
                proposal.action,
                children=payload.children,
                parent_branch_id=branch.branch_id,
            )
            state.decompose_records.append(
                {
                    "attempt_index": state.attempt_index,
                    "branch_id": branch.branch_id,
                    "obligation_id": branch.obligation_id,
                    "strategy": payload.strategy,
                    "children": [child.to_dict() for child in payload.children],
                }
            )
            logger.info(
                "Decompose executed: task_id=%s obligation=%s children=%d",
                task.task_id,
                branch.obligation_id,
                len(payload.children),
            )
        return workspace, True

    def _run_argument(
        self,
        task: ProofTask,
        branch: ProofBranch,
        proposals: list[StructuredActionProposal],
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[ProofWorkspace, bool]:
        for proposal in proposals:
            proposal = self._finalize_kind(proposal, branch)
            ok, errors = proposal.validate()
            if not ok:
                logger.warning("Argument proposal invalid, skipping: %s", errors)
                continue
            action = proposal.action
            if action.kind is SearchActionKind.PROPOSE_ARGUMENT:
                payload = proposal.payload
                assert isinstance(payload, ProposeArgumentPayload)
                workspace = apply_argument(
                    workspace,
                    action,
                    branch_id=branch.branch_id,
                    new_steps=payload.steps,
                    new_alignments=payload.alignments,
                )
            else:
                payload = proposal.payload
                assert isinstance(payload, RefineArgumentPayload)
                workspace = apply_argument(
                    workspace,
                    action,
                    branch_id=branch.branch_id,
                    refined_steps=payload.steps,
                    refined_alignments=payload.alignments,
                )
            state.argument_records.append(
                {
                    "attempt_index": state.attempt_index,
                    "branch_id": branch.branch_id,
                    "kind": action.kind.value,
                    "steps": [step.to_dict() for step in payload.steps],
                }
            )
            logger.info(
                "Argument edit executed: task_id=%s kind=%s steps=%d",
                task.task_id,
                action.kind.value,
                len(payload.steps),
            )
        return workspace, True

    def _run_change_representation(
        self,
        task: ProofTask,
        branch: ProofBranch,
        proposals: list[StructuredActionProposal],
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[ProofWorkspace, bool]:
        for proposal in proposals:
            proposal = self._finalize_kind(proposal, branch)
            ok, errors = proposal.validate()
            if not ok:
                logger.warning("Representation proposal invalid, skipping: %s", errors)
                continue
            current = branch_by_id(workspace, branch.branch_id)
            if current is None or current.status != BranchStatus.ACTIVE:
                _record_skipped(state, proposal)
                continue
            payload = proposal.payload
            assert isinstance(payload, ChangeRepresentationPayload)
            workspace = apply_change_representation(
                workspace,
                proposal.action,
                branch_id=branch.branch_id,
                argument_steps=payload.argument,
                alignments=payload.alignments,
            )
            state.representation_records.append(
                {
                    "attempt_index": state.attempt_index,
                    "parent_branch_id": branch.branch_id,
                    "step_count": len(payload.argument),
                }
            )
            logger.info(
                "Representation change executed: task_id=%s parent=%s",
                task.task_id,
                branch.branch_id,
            )
        return workspace, True

    def _select_test_action(self, branch: ProofBranch) -> SearchAction | None:
        kind_rank = {
            SearchActionKind.RUN_CAPABILITY_TEST: 0,
            SearchActionKind.DECOMPOSE: 1,
            SearchActionKind.PROPOSE_ARGUMENT: 2,
            SearchActionKind.REFINE_ARGUMENT: 2,
            SearchActionKind.CHANGE_REPRESENTATION: 2,
            SearchActionKind.IMPLEMENT: 3,
            SearchActionKind.REPAIR_IMPLEMENTATION: 3,
        }
        candidates: list[tuple[int, float, SearchAction]] = []
        for hypothesis in branch.failure_hypotheses:
            for test in hypothesis.proposed_tests:
                if test.target_branch_id != branch.branch_id:
                    continue
                candidates.append((kind_rank.get(test.kind, 4), -hypothesis.confidence, test))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _prioritize_selected_test(
        self,
        branch: ProofBranch,
        proposals: tuple[StructuredActionProposal, ...],
    ) -> tuple[StructuredActionProposal, ...]:
        selected = self._select_test_action(branch)
        if selected is None:
            return proposals
        matching: list[StructuredActionProposal] = []
        other: list[StructuredActionProposal] = []
        for proposal in proposals:
            finalized = self._finalize_kind(proposal, branch)
            (matching if finalized.action.kind is selected.kind else other).append(
                proposal
            )
        return tuple((*matching, *other)) if matching else proposals

    def _fold_failure_hypotheses(
        self,
        branch: ProofBranch,
        proposals: list[StructuredActionProposal],
        workspace: ProofWorkspace,
    ) -> ProofWorkspace:
        hypotheses: list = []
        for proposal in proposals:
            hypotheses.extend(proposal.metadata.get(FAILURE_HYPOTHESES_KEY, ()))
        if not hypotheses:
            return workspace
        return apply_failure_hypotheses(
            workspace, branch_id=branch.branch_id, hypotheses=hypotheses
        )


def _record_skipped(
    state: _StructuredRunState, proposal: StructuredActionProposal
) -> None:
    state.skipped_proposals.append(
        {
            "attempt_index": state.attempt_index,
            "kind": proposal.action.kind.value,
            "rationale": proposal.action.rationale,
        }
    )
