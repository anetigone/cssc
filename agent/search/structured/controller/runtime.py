"""Runtime helpers mixed into :class:`StructuredController`."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent.agents.context import ContextSummarizer
from agent.proof_system.base import CandidateEdit, ProofTask
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
from agent.search.action import ActionGenerationRequest
from agent.search.controller.context import summarize_context
from agent.search.safety import SafetyVerdict
from ..branch_ops import action_rationale, root_branch_id
from ..projection import build_context_projection
from ..proposal import (
    LEGACY_ACTION_KEY,
    LEGACY_KIND_DEFERRED,
    StructuredActionProposal,
)
from ..run_state import _StructuredRunState


class StructuredControllerRuntimeMixin:
    """Shared rendering, generation, and safety-review helpers."""

    context_summarizer: ContextSummarizer | None

    def _has_blocked_obligation(self, workspace: ProofWorkspace) -> bool:
        return any(
            obligation.status == ObligationStatus.BLOCKED
            for obligation in workspace.obligation_graph.active()
        )

    def _initial_workspace(self, task: ProofTask) -> ProofWorkspace:
        workspace = initialize_from_task(task)
        root_branch = ProofBranch(
            branch_id=root_branch_id(task),
            obligation_id=task.task_id,
            obligation_version=1,
            status=BranchStatus.ACTIVE,
        )
        return workspace.successor(
            branches=(root_branch,),
            status=WorkspaceStatus.SEARCHING,
        )

    def _finalize_kind(
        self, proposal: StructuredActionProposal, branch: ProofBranch
    ) -> StructuredActionProposal:
        if proposal.metadata.get(LEGACY_KIND_DEFERRED):
            kind = (
                SearchActionKind.IMPLEMENT
                if branch.last_action is None
                else SearchActionKind.REPAIR_IMPLEMENTATION
            )
            allowed_mutations = DEFAULT_ALLOWED_MUTATIONS[kind]
            rationale = action_rationale(kind, branch)
        else:
            kind = proposal.action.kind
            allowed_mutations = proposal.action.allowed_mutations
            rationale = proposal.action.rationale
        return replace(
            proposal,
            action=SearchAction(
                kind=kind,
                target_branch_id=branch.branch_id,
                allowed_mutations=allowed_mutations,
                rationale=rationale,
            ),
        )

    def _proposal_edit(
        self,
        proposal: StructuredActionProposal,
        proof_text: str,
        branch: ProofBranch,
    ) -> CandidateEdit:
        metadata = dict(proposal.metadata)
        if proposal.score is not None:
            metadata["score"] = proposal.score
        return CandidateEdit(
            text=proof_text,
            action=proposal.metadata.get(LEGACY_ACTION_KEY, "model_complete"),
            parent_node_id=branch.branch_id,
            metadata=metadata,
        )

    def _generate(
        self,
        task: ProofTask,
        branch: ProofBranch,
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[StructuredActionProposal, ...]:
        request = ActionGenerationRequest(
            task=task,
            attempt_index=state.attempt_index,
            max_candidates=self.config.max_candidates_per_model_call,
            metadata=self._generation_metadata(task, branch, workspace, state),
        )
        return tuple(self.action_generator.generate(request))

    def _generation_metadata(
        self,
        task: ProofTask,
        branch: ProofBranch,
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> dict[str, Any]:
        projection = build_context_projection(workspace, branch.branch_id)
        current = projection.current_obligation
        previous_attempt = None
        if projection.observations:
            previous_attempt = {
                "branch_id": branch.branch_id,
                "proof_text": projection.lean_artifact_proof_body,
                "observations": [
                    {
                        "category": obs.category,
                        "message": obs.message,
                        "goal_fingerprint": obs.goal_fingerprint,
                    }
                    for obs in projection.observations
                ],
            }
        selected_test_action = self._select_test_action(branch)
        return {
            "proof_phase": "implement" if branch.last_action is None else "repair",
            "branch_id": branch.branch_id,
            "branch_obligation": (
                {
                    "obligation_id": current.obligation_id,
                    "lean_statement": current.lean_statement,
                    "statement_nl": current.statement_nl,
                }
                if current is not None
                else None
            ),
            "previous_attempt": previous_attempt,
            "verified_facts": tuple(
                {"obligation_id": fact.obligation_id, "statement": fact.statement}
                for fact in projection.accepted_facts
            ),
            "structured_projection": projection.to_dict(),
            "retrieved_results": state.current_retrieved,
            "retrieved_history": tuple(state.retrieved_history),
            "summarized_context": summarize_context(
                task,
                state,
                self.context_summarizer,
                previous_attempt,
            ),
            "selected_test_action": (
                selected_test_action.to_dict()
                if selected_test_action is not None
                else None
            ),
            "structured_workspace_version": workspace.version,
            "budget": self.budget.snapshot(),
        }

    def _render_target(
        self,
        task: ProofTask,
        workspace: ProofWorkspace,
        branch: ProofBranch,
        proof_text: str,
    ) -> tuple[ProofTask, str]:
        if branch.obligation_id in workspace.root_obligation_ids:
            return task, proof_text
        obligation = workspace.obligation_graph.by_id(branch.obligation_id)
        if obligation is None or not obligation.lean_statement:
            return task, proof_text
        helper_task = ProofTask(
            task_id=branch.obligation_id,
            source_template=obligation.lean_statement,
            hole_marker=task.hole_marker,
            imports=task.imports,
        )
        return helper_task, obligation.lean_statement.replace(
            task.hole_marker, proof_text
        )

    def _check(
        self,
        task: ProofTask,
        edit: Any,
        state: _StructuredRunState,
    ) -> Any:
        source = self.adapter.render_candidate(task, edit)
        materialized = self.workspace.write_candidate(
            task,
            edit,
            source,
            extension=self.config.candidate_extension,
        )
        budget_slice = self.budget.reserve_check()
        if self.check_workspace is None:
            return self.adapter.check(materialized.path, budget_slice)
        with self.check_workspace.materialize_candidate(
            task,
            candidate_id=materialized.candidate_id,
            source=source,
            extension=self.config.candidate_extension,
        ) as check_candidate:
            check_result = self.adapter.check(check_candidate.path, budget_slice)
        return replace(check_result, candidate_file=materialized.path)

    def _review(
        self,
        task: ProofTask,
        edit: Any,
        check_result: Any,
        state: _StructuredRunState,
    ) -> SafetyVerdict:
        if not check_result.accepted:
            return SafetyVerdict(accepted=False)
        source = self.adapter.render_candidate(task, edit)
        verdict = self.safety_reviewer.accepts(task, source, check_result)
        if not verdict.accepted:
            state.safety_rejections.append(
                {
                    "attempt_index": state.attempt_index,
                    "reasons": verdict.reasons,
                    "metadata": dict(verdict.metadata),
                }
            )
        return verdict
