"""Structural action executors mixed into :class:`StructuredController`."""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from agent.proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ProofTask,
)
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

_DECLARATION_BEFORE_HOLE_RE = re.compile(
    r"^[ \t]*(?:private[ \t]+)?(?:noncomputable[ \t]+)?"
    r"(?:theorem|lemma|def|example)[ \t]+",
    re.MULTILINE,
)


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
                logger.info(
                    "Capability audit skipped due to check budget: task_id=%s branch=%s",
                    task.task_id,
                    branch.branch_id,
                )
                return workspace, True
            proposal = self._finalize_kind(proposal, branch)
            payload = proposal.payload
            assert isinstance(payload, CapabilityTestPayload)
            logger.debug(
                "Capability audit starting: task_id=%s branch=%s requirement=%s signature_chars=%d",
                task.task_id,
                branch.branch_id,
                payload.requirement,
                len(payload.signature),
            )
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
            source = _capability_probe_source(task, payload.signature)
            check_result = self._check_source(
                task, edit, source, state, force_subprocess=True
            )
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
            logger.info(
                "Capability audit completed: task_id=%s branch=%s attempt_index=%d "
                "accepted=%s category=%s workspace_version=%d",
                task.task_id,
                branch.branch_id,
                attempt_index,
                check_result.accepted,
                check_result.category.value,
                workspace.version,
            )
            if (
                self.config.stop_on_tool_unavailable
                and check_result.category == DiagnosticCategory.TOOL_UNAVAILABLE
            ):
                state.stop_reason = "tool_unavailable"
                logger.info(
                    "Capability audit stopped on tool unavailable: task_id=%s branch=%s",
                    task.task_id,
                    branch.branch_id,
                )
                return workspace, True
            updated = branch_by_id(workspace, branch.branch_id)
            if updated is None or updated.status != BranchStatus.ACTIVE:
                logger.info(
                    "Capability audit retired branch: task_id=%s branch=%s status=%s",
                    task.task_id,
                    branch.branch_id,
                    None if updated is None else updated.status.value,
                )
                return workspace, True
        return workspace, False

    def _check_source(
        self,
        task: ProofTask,
        edit: CandidateEdit,
        source: str,
        state: _StructuredRunState,
        *,
        force_subprocess: bool = False,
    ) -> CheckResult:
        materialized = self.workspace.write_candidate(
            task,
            edit,
            source,
            extension=self.config.candidate_extension,
        )
        budget_slice = self.budget.reserve_check()
        logger.debug(
            "Structured checker reserved for source: task_id=%s candidate_id=%s "
            "checks_used=%d remaining_checks=%s timeout=%s",
            task.task_id,
            materialized.candidate_id,
            self.budget.snapshot().checks_used,
            self.budget.snapshot().remaining_checks,
            budget_slice.timeout_seconds,
        )
        adapter = self.adapter
        if force_subprocess:
            subprocess_clone = getattr(adapter, "subprocess_clone", None)
            if callable(subprocess_clone):
                adapter = subprocess_clone()
        if self.check_workspace is None:
            return adapter.check(materialized.path, budget_slice)
        with self.check_workspace.materialize_candidate(
            task,
            candidate_id=materialized.candidate_id,
            source=source,
            extension=self.config.candidate_extension,
        ) as check_candidate:
            check_result = adapter.check(check_candidate.path, budget_slice)
        return replace(check_result, candidate_file=materialized.path)

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
                logger.debug(
                    "Decompose skipped for inactive branch: task_id=%s branch=%s status=%s",
                    task.task_id,
                    branch.branch_id,
                    None if current is None else current.status.value,
                )
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
                logger.debug(
                    "Representation change skipped for inactive branch: task_id=%s branch=%s status=%s",
                    task.task_id,
                    branch.branch_id,
                    None if current is None else current.status.value,
                )
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
        logger.info(
            "Folding failure hypotheses: branch=%s count=%d",
            branch.branch_id,
            len(hypotheses),
        )
        updated = apply_failure_hypotheses(
            workspace, branch_id=branch.branch_id, hypotheses=hypotheses
        )
        logger.debug(
            "Failure hypotheses folded: branch=%s workspace_version=%d",
            branch.branch_id,
            updated.version,
        )
        return updated


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


def _capability_probe_source(task: ProofTask, signature: str) -> str:
    """Build a standalone Lean probe in the task's surrounding context."""
    marker_index = task.source_template.find(task.hole_marker)
    prefix = (
        task.source_template
        if marker_index < 0
        else task.source_template[:marker_index]
    )
    declaration_start = None
    for match in _DECLARATION_BEFORE_HOLE_RE.finditer(prefix):
        declaration_start = match.start()
    context = prefix[:declaration_start] if declaration_start is not None else prefix
    parts: list[str] = []
    if task.imports:
        parts.append("\n".join(f"import {module}" for module in task.imports))
    if context.strip():
        parts.append(context.rstrip())
    parts.append(_capability_probe_command(signature))
    return "\n\n".join(part for part in parts if part).rstrip() + "\n"


def _capability_probe_command(signature: str) -> str:
    stripped = signature.strip()
    match = re.fullmatch(r"#check\s+(.+)", stripped, flags=re.DOTALL)
    if match:
        return (
            "set_option autoImplicit false\n"
            f"def __cssc_capability_probe__ := {match.group(1).strip()}"
        )
    return stripped
