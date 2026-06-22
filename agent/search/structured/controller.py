"""Structured execution mode controller: frontier / AND-OR search.

The :class:`StructuredController` drives the structured search state introduced
in Phases 3-5. It has the same constructor shape as the minimal
:class:`ProofController` (so :func:`agent.search.factory.build_controller`
switches between them at zero cost) and reuses the shared budget, metrics, and
trace pipeline, but its loop is the structured one from ``tmp/plan1.md`` §12:

    while frontier.has_work() and budget remains:
        node   = frontier.pop()
        action = pick_action(node)         # IMPLEMENT, then REPAIR_IMPLEMENTATION
        result = execute(action, node)     # generate -> render -> check -> safety
        workspace = reducer.apply(workspace, action, result)
        frontier.update(workspace, ...)
        if solution_tracker.has_complete_solution(workspace):
            return assemble_and_finalize(workspace)

Action selection reuses the existing :class:`ActionGenerator` (it yields proof
bodies); the controller wraps each body in a deterministic
IMPLEMENT / REPAIR_IMPLEMENTATION :class:`SearchAction` whose ``allowed_mutations``
come from :data:`DEFAULT_ALLOWED_MUTATIONS`. No new model protocol is introduced
here — that is Phase 7's job.

Budget discipline: every IMPLEMENT attempt costs one model call and one check;
the final assembly costs one additional check (reserved explicitly before
calling the assembler, which receives a ``budget_slice`` but does not reserve
its own).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from ...proof_system.assembler import ArtifactAssembler
from ...proof_system.base import (
    DiagnosticCategory,
    ProofSystemAdapter,
    ProofTask,
)
from ...proof_system.workspace import (
    DEFAULT_ALLOWED_MUTATIONS,
    BranchStatus,
    ProofBranch,
    ProofWorkspace,
    SearchAction,
    SearchActionKind,
    WorkspaceStatus,
    initialize_from_task,
)
from ..action import ActionCandidate, ActionGenerationRequest, ActionGenerator
from ..budget import BudgetConfig, BudgetManager
from ..controller.types import (
    AttemptRecord,
    ControllerConfig,
    ControllerResult,
    Retriever,
)
from ..controller.context import maybe_retrieve, summarize_context
from ..execution import ExecutionMode
from ..metrics import attempt_metric
from ..safety import SafetyReviewer, SafetyVerdict, StatementSafetyReviewer
from ...agents.context import ContextSummarizer
from ...runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace
from .frontier import Frontier
from .reducer import StructuredActionResult, apply
from .run_state import _StructuredRunState, build_structured_result
from .solution_tracker import has_complete_solution, select_solution

logger = logging.getLogger(__name__)


class StructuredController:
    """Coordinate the structured AND-OR search over one task.

    Single Proof Agent, structured state: the controller pops frontier nodes,
    asks the action generator for a proof body, checks it, folds the outcome
    into the immutable workspace via the reducer, and runs a final whole-source
    assembly once a complete solution exists. It never makes mathematical
    decisions and never switches execution modes mid-run.
    """

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
        self.action_generator = action_generator
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

        while (
            frontier.has_work()
            and self.budget.can_check()
            and self.budget.can_call_model()
        ):
            node = frontier.pop()
            branch = _branch_by_id(workspace, node.branch_id)
            if branch is None or branch.status != BranchStatus.ACTIVE:
                continue

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
            self.budget.reserve_model_call()
            candidates = self._generate(task, branch, workspace, state)
            if not candidates:
                workspace = _block_branch(workspace, branch.branch_id)
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    state.stop_reason = "no_actions"
                continue

            workspace, candidate_branches = _expand_candidate_branches(
                workspace,
                branch,
                len(candidates[: self.config.max_candidates_per_model_call]),
                state.attempt_index,
            )
            stop_for_tool = False
            attempted_branch_ids: list[str] = []
            for candidate, candidate_branch in zip(candidates, candidate_branches):
                if not self.budget.can_check():
                    state.stop_reason = "budget:checks"
                    break
                action = self._pick_action(candidate_branch)
                edit = _edit_with_structured_metadata(
                    candidate.to_edit(parent_node_id=branch.branch_id),
                    action,
                    candidate_branch,
                )
                check_result = self._check(task, edit, state)
                attempted_branch_ids.append(candidate_branch.branch_id)
                safety_verdict = self._review(task, edit, check_result, state)
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
                        proof_text=candidate.proof_text,
                        # LeanArtifact.source is an obligation snippet; the
                        # assembler renders it into the task exactly once.
                        source=candidate.proof_text,
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
                    return self._assemble_and_finalize(task, workspace, state)

            frontier.update(
                workspace,
                branch.branch_id,
                accepted=False,
                attempted_branch_ids=tuple(attempted_branch_ids),
            )
            if stop_for_tool or state.stop_reason == "budget:checks":
                break

        if state.stop_reason == "budget":
            reason = self.budget.exhausted_reason()
            if reason is not None:
                state.stop_reason = f"budget:{reason}"
        return build_structured_result(
            state,
            task,
            workspace,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=ExecutionMode.STRUCTURED,
            budget=self.budget,
            safety_reviewer=self.safety_reviewer,
        )

    def _initial_workspace(self, task: ProofTask) -> ProofWorkspace:
        workspace = initialize_from_task(task)
        root_branch = ProofBranch(
            branch_id=_root_branch_id(task),
            obligation_id=task.task_id,
            obligation_version=1,
            status=BranchStatus.ACTIVE,
        )
        return workspace.successor(
            branches=(root_branch,),
            status=WorkspaceStatus.SEARCHING,
        )

    def _pick_action(self, branch: ProofBranch) -> SearchAction:
        kind = (
            SearchActionKind.IMPLEMENT
            if branch.last_action is None
            else SearchActionKind.REPAIR_IMPLEMENTATION
        )
        return SearchAction(
            kind=kind,
            target_branch_id=branch.branch_id,
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[kind],
            rationale=_action_rationale(kind, branch),
        )

    def _generate(
        self,
        task: ProofTask,
        branch: ProofBranch,
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[ActionCandidate, ...]:
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
        obligation = workspace.obligation_graph.by_id(branch.obligation_id)
        previous_attempt = None
        if branch.observations:
            previous_attempt = {
                "branch_id": branch.branch_id,
                "observations": [
                    {
                        "category": obs.category,
                        "message": obs.message,
                        "goal_fingerprint": obs.goal_fingerprint,
                    }
                    for obs in branch.observations
                ],
            }
        summarized_context = summarize_context(
            task,
            state,
            self.context_summarizer,
            previous_attempt,
        )
        return {
            "proof_phase": (
                "implement" if branch.last_action is None else "repair"
            ),
            "branch_id": branch.branch_id,
            "branch_obligation": (
                {
                    "obligation_id": obligation.obligation_id,
                    "lean_statement": obligation.lean_statement,
                    "statement_nl": obligation.statement_nl,
                }
                if obligation is not None
                else None
            ),
            "previous_attempt": previous_attempt,
            "retrieved_results": state.current_retrieved,
            "retrieved_history": tuple(state.retrieved_history),
            "summarized_context": summarized_context,
            "structured_workspace_version": workspace.version,
            "budget": self.budget.snapshot(),
        }

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
            check_result = self.adapter.check(materialized.path, budget_slice)
        else:
            with self.check_workspace.materialize_candidate(
                task,
                candidate_id=materialized.candidate_id,
                source=source,
                extension=self.config.candidate_extension,
            ) as check_candidate:
                check_result = self.adapter.check(check_candidate.path, budget_slice)
            check_result = replace(
                check_result, candidate_file=materialized.path
            )
        return check_result

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

    def _assemble_and_finalize(
        self,
        task: ProofTask,
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> ControllerResult:
        if not self.budget.can_check():
            state.stop_reason = "budget:checks"
            return build_structured_result(
                state,
                task,
                workspace,
                accepted=False,
                stop_reason=state.stop_reason,
                execution_mode=ExecutionMode.STRUCTURED,
                budget=self.budget,
                safety_reviewer=self.safety_reviewer,
            )
        budget_slice = self.budget.reserve_check()
        solution_branches = select_solution(workspace)
        artifacts = {
            branch.obligation_id: branch.lean_artifact
            for branch in solution_branches
            if branch.lean_artifact is not None
        }
        assembly = self.assembler.assemble(
            workspace,
            artifacts,
            adapter=self.adapter,
            task=task,
            check_workspace=self.check_workspace,
            budget_slice=budget_slice,
            safety_reviewer=self.safety_reviewer,
        )
        logger.info(
            "Structured assembly: task_id=%s accepted=%s errors=%d",
            task.task_id,
            assembly.accepted,
            len(assembly.errors),
        )
        if assembly.accepted:
            workspace = workspace.successor(status=WorkspaceStatus.ACCEPTED)
            return build_structured_result(
                state,
                task,
                workspace,
                accepted=True,
                stop_reason="accepted",
                execution_mode=ExecutionMode.STRUCTURED,
                budget=self.budget,
                safety_reviewer=self.safety_reviewer,
            )
        state.stop_reason = "assembly_failed"
        return build_structured_result(
            state,
            task,
            workspace,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=ExecutionMode.STRUCTURED,
            budget=self.budget,
            safety_reviewer=self.safety_reviewer,
        )


def _root_branch_id(task: ProofTask) -> str:
    return f"{task.task_id}:root"


def _branch_by_id(workspace: ProofWorkspace, branch_id: str) -> ProofBranch | None:
    for branch in workspace.branches:
        if branch.branch_id == branch_id:
            return branch
    return None


def _action_rationale(kind: SearchActionKind, branch: ProofBranch) -> str:
    if kind == SearchActionKind.IMPLEMENT:
        return f"initial implementation for branch {branch.branch_id}"
    return f"repair implementation for branch {branch.branch_id}"


def _block_branch(workspace: ProofWorkspace, branch_id: str) -> ProofWorkspace:
    branches = tuple(
        replace(branch, status=BranchStatus.BLOCKED)
        if branch.branch_id == branch_id
        else branch
        for branch in workspace.branches
    )
    return workspace.successor(branches=branches)


def _expand_candidate_branches(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    count: int,
    batch_index: int,
) -> tuple[ProofWorkspace, tuple[ProofBranch, ...]]:
    """Materialize one branch per candidate without overwriting alternatives."""
    if count <= 1:
        return workspace, (branch,)
    alternatives = [branch]
    existing_ids = {item.branch_id for item in workspace.branches}
    for candidate_index in range(1, count):
        candidate_id = f"{branch.branch_id}.c{batch_index}.{candidate_index}"
        suffix = 0
        while candidate_id in existing_ids:
            suffix += 1
            candidate_id = (
                f"{branch.branch_id}.c{batch_index}.{candidate_index}.{suffix}"
            )
        existing_ids.add(candidate_id)
        alternatives.append(
            replace(
                branch,
                branch_id=candidate_id,
                parent_branch_id=branch.branch_id,
                lean_artifact=None,
                last_action=None,
                status=BranchStatus.ACTIVE,
            )
        )
    return (
        workspace.successor(branches=(*workspace.branches, *alternatives[1:])),
        tuple(alternatives),
    )


def _edit_with_structured_metadata(edit: Any, action: SearchAction, branch: ProofBranch) -> Any:
    metadata = dict(edit.metadata)
    metadata["structured_action_kind"] = action.kind.value
    metadata["structured_branch_id"] = branch.branch_id
    return replace(edit, metadata=metadata)
