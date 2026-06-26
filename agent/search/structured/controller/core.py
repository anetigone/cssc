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
from agent.search.action import ActionGenerationRequest, ActionGenerator
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
    """Coordinate the structured AND-OR search over one task.

    Single Proof Agent, structured state: the controller pops frontier nodes,
    asks the action generator for a typed proposal, checks it, folds the
    outcome into the immutable workspace via the reducer, and runs a final
    whole-source assembly once a complete solution exists. It never makes
    mathematical decisions and never switches execution modes mid-run.

    The action generator is a :class:`StructuredActionGenerator` (typed
    proposals). A legacy :class:`ActionGenerator` (returns proof-body
    candidates) is accepted and adapted at construction via
    :func:`adapt_legacy_generator`, so baseline comparability is preserved.
    Phase 7.2 only *executes* IMPLEMENT / REPAIR_IMPLEMENTATION proposals;
    DECOMPOSE / RUN_CAPABILITY_TEST are valid, serialized proposal types but
    are recorded into ``state.skipped_proposals`` and not yet driven (those
    executors are Phase 7.3 / 7.4).
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
        # Normalize the generator to the typed protocol. Native structured
        # generators declare ``_is_structured_generator``; a legacy
        # ActionGenerator is wrapped so every proof-body candidate becomes an
        # IMPLEMENT proposal. ``adapt_legacy_generator`` is idempotent.
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

        while (
            frontier.has_work()
            and self.budget.can_check()
            and self.budget.can_call_model()
        ):
            node = frontier.pop()
            branch = branch_by_id(workspace, node.branch_id)
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
            proposals = self._generate(task, branch, workspace, state)
            if not proposals:
                workspace = block_branch(workspace, branch.branch_id)
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    state.stop_reason = "no_actions"
                continue

            # Phase 7.6: a native generator may attach competing failure
            # hypotheses to its proposals (under FAILURE_HYPOTHESES_KEY). Fold
            # them onto the branch now — after any prior failure's observations
            # are already on it, so the reducer's evidence-resolution check
            # passes. Hypotheses are model data riding the already-budgeted
            # generation, not a separate model call. No-op when nothing is
            # attached (the legacy adapter path always takes this branch).
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
                    # Phase 7.3: capability audits are executed (real Lean probe
                    # → observation → maybe block). They run before any
                    # IMPLEMENT candidate so a missing capability blocks the
                    # route before the model wastes an implementation on it.
                    capability_proposals.append(proposal)
                    continue
                if action.kind is SearchActionKind.DECOMPOSE:
                    # Phase 7.4: decomposition is a structural move (split the
                    # obligation into helpers). It is executed before any
                    # IMPLEMENT candidate on this branch, and unlike an
                    # implementation it consumes no check and no model call.
                    decompose_proposals.append(proposal)
                    continue
                if action.kind in (
                    SearchActionKind.PROPOSE_ARGUMENT,
                    SearchActionKind.REFINE_ARGUMENT,
                ):
                    # Phase 7.6: argument-layer edits are structural (no check,
                    # no safety). They run before IMPLEMENT so the branch's
                    # argument/alignment is current before any realization is
                    # spent against it.
                    argument_proposals.append(proposal)
                    continue
                if action.kind is SearchActionKind.CHANGE_REPRESENTATION:
                    # Phase 7.6: a representation switch forks a new branch and
                    # supersedes this one, so it must run before IMPLEMENT and
                    # the controller must not then spend candidates here.
                    representation_proposals.append(proposal)
                    continue
                if action.kind not in (
                    SearchActionKind.IMPLEMENT,
                    SearchActionKind.REPAIR_IMPLEMENTATION,
                ):
                    # Boundary: any other valid, serialized proposal kind whose
                    # executor has not landed yet. Record what the generator
                    # emitted for the trace, then skip without changing the
                    # workspace.
                    state.skipped_proposals.append(
                        {
                            "attempt_index": state.attempt_index,
                            "kind": action.kind.value,
                            "rationale": action.rationale,
                        }
                    )
                    continue
                executable_proposals.append(proposal)

            if capability_proposals:
                workspace, stop_for_capability = self._run_capability_audits(
                    task, branch, capability_proposals, workspace, state
                )
                if stop_for_capability or state.stop_reason != "budget":
                    frontier.update(workspace, branch.branch_id, accepted=False)
                    if not frontier.has_work():
                        if not state.stop_reason:
                            state.stop_reason = "no_actions"
                    continue

            if decompose_proposals:
                # Phase 7.4: split the branch's obligation into helpers. The
                # branch is retired (superseded) by the reducer, so the
                # controller must not then spend IMPLEMENT candidates on it this
                # iteration — refresh the frontier and continue. A later
                # iteration pops a child branch (now ready) instead.
                workspace, _ = self._run_decompose(
                    task, branch, decompose_proposals, workspace, state
                )
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    if not state.stop_reason:
                        state.stop_reason = "no_actions"
                continue

            if argument_proposals:
                # Phase 7.6: edit the branch's argument/alignment layer in
                # place. The branch stays ACTIVE but its argument graph
                # changed, so refresh the frontier and continue rather than
                # spending IMPLEMENT candidates on a stale view this iteration.
                workspace, _ = self._run_argument(
                    task, branch, argument_proposals, workspace, state
                )
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    if not state.stop_reason:
                        state.stop_reason = "no_actions"
                continue

            if representation_proposals:
                # Phase 7.6: a representation switch forks a new branch and
                # supersedes this one — the controller must not spend IMPLEMENT
                # candidates on the now-superseded branch. Refresh and continue.
                workspace, _ = self._run_change_representation(
                    task, branch, representation_proposals, workspace, state
                )
                frontier.update(workspace, branch.branch_id, accepted=False)
                if not frontier.has_work():
                    if not state.stop_reason:
                        state.stop_reason = "no_actions"
                continue

            executable_proposals = executable_proposals[
                : self.config.max_candidates_per_model_call
            ]

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
                    break
                proposal = self._finalize_kind(proposal, candidate_branch)
                action = proposal.action
                proof_text = proposal.payload.proof_text  # type: ignore[union-attr]
                # Render against the right obligation. A root obligation fills
                # the task's proof hole (and its artifact source is the proof
                # body, the baseline). A helper obligation is a standalone
                # declaration checked on its own: render its lean_statement with
                # the proof body in its hole, so the helper is verified
                # independently and its artifact source is the full declaration
                # a parent proof reuses by name.
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
                        # The artifact source is the rendered text the assembler
                        # and the fact layer reuse: the proof body for a root,
                        # the full declaration for a helper.
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
            if stop_for_tool or state.stop_reason == "budget:checks":
                break

        if state.stop_reason == "budget":
            reason = self.budget.exhausted_reason()
            if reason is not None:
                state.stop_reason = f"budget:{reason}"
        if state.stop_reason == "budget" and not frontier.has_work():
            # The loop ended because no branch is ready (every ACTIVE branch's
            # obligation has an un-accepted or blocked dependency), not because
            # of budget. This is the multi-obligation terminal: helpers left
            # open or blocked make their parent un-attackable.
            state.stop_reason = "no_ready_work"
        if state.stop_reason == "no_ready_work" and self._has_blocked_obligation(
            workspace
        ):
            # Sharpen the terminal reason: at least one active obligation is
            # BLOCKED (a mechanical dead-end), distinguishing it from a run that
            # simply ran out of ready work with everything still open.
            state.stop_reason = "blocked"
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
