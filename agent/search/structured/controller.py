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
    CandidateEdit,
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
from ..action import ActionGenerationRequest, ActionGenerator
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
from .branch_ops import (
    action_rationale,
    block_branch,
    branch_by_id,
    edit_with_structured_metadata,
    expand_candidate_branches,
    root_branch_id,
)
from .finalize import assemble_and_finalize
from .frontier import Frontier
from .projection import build_context_projection
from .proposal import (
    LEGACY_ACTION_KEY,
    LEGACY_KIND_DEFERRED,
    CapabilityTestPayload,
    DecomposePayload,
    StructuredActionProposal,
    adapt_legacy_generator,
)
from .reducer import StructuredActionResult, apply, apply_decompose
from .run_state import _StructuredRunState, build_structured_result
from .solution_tracker import has_complete_solution

logger = logging.getLogger(__name__)


class StructuredController:
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

            executable_proposals: list[StructuredActionProposal] = []
            capability_proposals: list[StructuredActionProposal] = []
            decompose_proposals: list[StructuredActionProposal] = []
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
        """Set IMPLEMENT vs REPAIR_IMPLEMENTATION on deferred (legacy) proposals.

        A legacy :class:`ActionGenerator` cannot see branch state at
        ``generate`` time, so the adapter emits an ``IMPLEMENT`` placeholder
        flagged with :data:`~.proposal.LEGACY_KIND_DEFERRED`. Once the candidate
        branch is materialized we know ``branch.last_action``, which is the
        exact rule the old ``_pick_action`` used. Native structured proposals
        already carry a finalized kind/scope/rationale; they are still retargeted
        to the materialized branch so branch-local provenance stays valid.
        """
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
        """Build the :class:`CandidateEdit` an IMPLEMENT proposal renders.

        Replaces the old ``candidate.to_edit`` path: the proof body comes from
        the proposal payload, and ``action`` carries the legacy candidate's
        action string (``"static"`` for :class:`StaticActionGenerator`,
        ``"model_complete"`` for the chat generator) via
        :data:`~.proposal.LEGACY_ACTION_KEY`, defaulting to ``"model_complete"``.
        ``score`` is forwarded into metadata like :meth:`ActionCandidate.to_edit`.
        """
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
        # The projection is the single source of truth for structured context:
        # ``branch_obligation`` / ``verified_facts`` / ``previous_attempt`` are
        # derived from it so the summarizer, the prompt renderer, and the
        # richer ``structured_projection`` block all read identical evidence.
        projection = build_context_projection(workspace, branch.branch_id)
        current = projection.current_obligation

        # ``previous_attempt`` is what the ContextSummarizer consumes and what
        # the legacy prompt renderer revises; derive it from the deduped
        # projection observations so the summarizer can only compress evidence
        # the projection already carries — never a parallel set of facts.
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
                    "obligation_id": current.obligation_id,
                    "lean_statement": current.lean_statement,
                    "statement_nl": current.statement_nl,
                }
                if current is not None
                else None
            ),
            "previous_attempt": previous_attempt,
            "verified_facts": tuple(
                {
                    "obligation_id": fact.obligation_id,
                    "statement": fact.statement,
                }
                for fact in projection.accepted_facts
            ),
            "structured_projection": projection.to_dict(),
            "retrieved_results": state.current_retrieved,
            "retrieved_history": tuple(state.retrieved_history),
            "summarized_context": summarized_context,
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
        """Pick the task/source to render and check for ``branch``'s obligation.

        A root obligation fills the task's proof hole; its artifact source is
        the proof body (the baseline — the root statement already lives in the
        task template). A helper (decomposed child) is a standalone declaration:
        it is checked against its own ``lean_statement`` with the proof body in
        its hole, and its artifact source is the resulting full declaration.
        """
        if branch.obligation_id in workspace.root_obligation_ids:
            return task, proof_text
        obligation = workspace.obligation_graph.by_id(branch.obligation_id)
        if obligation is None or not obligation.lean_statement:
            # No statement to render independently: fall back to the root path.
            return task, proof_text
        helper_task = ProofTask(
            task_id=branch.obligation_id,
            source_template=obligation.lean_statement,
            hole_marker=task.hole_marker,
            imports=task.imports,
        )
        rendered = obligation.lean_statement.replace(task.hole_marker, proof_text)
        return helper_task, rendered

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

    def _run_capability_audits(
        self,
        task: ProofTask,
        branch: ProofBranch,
        proposals: list[StructuredActionProposal],
        workspace: ProofWorkspace,
        state: _StructuredRunState,
    ) -> tuple[ProofWorkspace, bool]:
        """Execute each ``RUN_CAPABILITY_TEST`` proposal against the checker.

        Each probe renders the payload's ``signature`` (replacing the proof
        hole, exactly like an IMPLEMENT candidate) and costs one check. A probe
        consumes no model call of its own — the signature is supplied by the
        generator, not regenerated. The outcome is folded through the reducer,
        which records a ``CAPABILITY_AUDIT`` observation and, on a missing
        capability, blocks the branch and obligation together.

        Returns the successor workspace and a flag meaning "stop this branch":
        ``True`` when the route was blocked (the reducer moved the branch out of
        ACTIVE, so no IMPLEMENT candidate should be spent on it). Budget
        exhaustion sets ``state.stop_reason`` and also stops the branch.
        """
        for proposal in proposals:
            if not self.budget.can_check():
                state.stop_reason = "budget:checks"
                return workspace, True
            proposal = self._finalize_kind(proposal, branch)
            payload = proposal.payload
            assert isinstance(payload, CapabilityTestPayload)
            signature = payload.signature
            metadata = {
                "capability_requirement": payload.requirement,
                "structured_action_kind": proposal.action.kind.value,
                "structured_branch_id": branch.branch_id,
            }
            edit = CandidateEdit(
                text=signature,
                action="capability_test",
                parent_node_id=branch.branch_id,
                metadata=metadata,
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
                    # A capability probe is never a proof: pass a rejecting
                    # verdict so the reducer's capability branch — which does
                    # not consult safety — is taken without registering a fact.
                    safety_verdict=SafetyVerdict(accepted=False),
                    proof_text=signature,
                    source=signature,
                    attempt_index=attempt_index,
                ),
            )
            logger.info(
                "Capability audit checked: task_id=%s requirement=%s "
                "category=%s accepted=%s",
                task.task_id,
                payload.requirement,
                check_result.category.value,
                check_result.accepted,
            )
            if (
                self.config.stop_on_tool_unavailable
                and check_result.category == DiagnosticCategory.TOOL_UNAVAILABLE
            ):
                state.stop_reason = "tool_unavailable"
                return workspace, True
            # If the reducer blocked the branch, the route is dead: stop
            # spending IMPLEMENT candidates on it this iteration.
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
        """Execute each ``DECOMPOSE`` proposal as a structural obligation split.

        Decomposition is not a proof: it consumes no check and no model call
        (the children's statements come from the generator's payload, not from a
        fresh generation). Each proposal is retargeted to the current branch,
        validated, and folded through :func:`apply_decompose`, which supersedes
        the branch's obligation version, retires the old parent-version branches,
        and seeds one ACTIVE branch per child plus one for the new parent.

        Returns the successor workspace and a flag meaning "stop this branch":
        always ``True`` here, because a decomposed branch is superseded — the
        controller must not spend IMPLEMENT candidates on it afterwards. Only the
        first decompose per branch takes effect: once the branch is superseded
        the remaining proposals target dead state and are recorded as skipped.
        """
        for proposal in proposals:
            proposal = self._finalize_kind(proposal, branch)
            ok, errors = proposal.validate()
            if not ok:
                logger.warning(
                    "Decompose proposal invalid, skipping: %s", errors
                )
                continue
            payload = proposal.payload
            assert isinstance(payload, DecomposePayload)
            current = branch_by_id(workspace, branch.branch_id)
            if current is None or current.status != BranchStatus.ACTIVE:
                # The branch was already superseded by an earlier decompose in
                # this same batch; record the rest as skipped rather than
                # targeting dead state.
                state.skipped_proposals.append(
                    {
                        "attempt_index": state.attempt_index,
                        "kind": proposal.action.kind.value,
                        "rationale": proposal.action.rationale,
                    }
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
