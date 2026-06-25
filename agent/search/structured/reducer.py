"""Deterministic reducer for structured workspace transitions.

The reducer is the *only* thing that advances a :class:`ProofWorkspace` during
a structured run. It is a pure function: given the current workspace, the
:class:`SearchAction` that was executed, and a
:class:`StructuredActionResult` carrying the checker + safety outcome, it
returns the next immutable workspace. Nothing here mutates in place.

Transitions (``tmp/plan1.md`` §5/§7/§9):

* accepted + safety-accepted → the branch becomes ACCEPTED, its artifact is
  pinned, and the obligation is registered as an accepted fact with provenance;
* accepted + safety-rejected → the branch stays ACTIVE, a safety observation
  is appended so the evidence is not lost;
* check-rejected → the branch stays ACTIVE, checker observations are appended
  and the artifact is retained as provenance (a failed realization does not
  negate its mathematical strategy).

Phase 7.3 adds the capability-audit transition: a ``RUN_CAPABILITY_TEST`` action
folds into a neutral :class:`Observation` (``CAPABILITY_AUDIT`` source). A
capability the checker reports as *missing* (unknown identifier / invalid
reference / tool unavailable) blocks the route — the branch **and** its
obligation go ``BLOCKED`` together. Any other outcome is recorded as evidence
but does not block: a capability audit may only block a route, never declare a
proposition wrong.

Phase 7.4 adds the decomposition transition (:func:`apply_decompose`), a
*structural* move with no checker outcome: it splits an obligation into helper
children via :meth:`ProofWorkspace.decompose`, retires the old parent-version
branches to ``SUPERSEDED`` in the same successor, and seeds one ACTIVE branch
per child plus one for the new parent version. Because decomposition produces no
proof, it consumes no check and registers no fact — facts arise only from a
later accepted ``IMPLEMENT`` on a child. The artifact contract is also fixed
here: a root artifact is a hole-filling ``PROOF_BODY`` whose fact statement is
the proof body (baseline), a helper artifact is a standalone ``DECLARATION``
whose fact statement is the helper's Lean declaration (so a parent prompt can
reuse the helper by name).

Phase 7.6 adds three more structural transitions for the argument /
representation layer (:func:`apply_argument`,
:func:`apply_change_representation`, :func:`apply_failure_hypotheses`). A
``PROPOSE_ARGUMENT`` / ``REFINE_ARGUMENT`` action edits the branch's argument
graph and its alignments in place — a step and its alignment land in the *same*
successor, because :meth:`ProofBranch.validate` requires every argument step to
carry an alignment link. A ``CHANGE_REPRESENTATION`` action forks a new
representation branch (``<parent>.rep<n>``) carrying a full replacement
argument + alignment layer, inherits the parent's observations as evidence, and
retires the parent to ``SUPERSEDED``. None of these touch the Lean checker or
safety review: an argument step is a mathematical claim, not an executable
proof. :func:`apply_failure_hypotheses` folds *competing* model-generated
failure hypotheses onto a branch, dropping any whose evidence / step / test
references do not resolve against the branch (a hypothesis is a model product;
the reducer only validates and attaches, never synthesizes). Every structural
transition pre-commit validates the resulting branch and no-ops on a malformed
payload so the workspace can never become invalid.

On repeated stall (same goal fingerprints across attempts) the branch is
retired to DORMANT; if a branch implementing for the first time keeps failing
on the same goals, a REPAIR_IMPLEMENTATION child branch is spawned so the
search can retry a different realization without overwriting the parent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Sequence

from ...proof_system.base import CheckResult, DiagnosticCategory
from ...proof_system.workspace import (
    AlignmentLink,
    AlignmentRelation,
    ArgumentGraph,
    ArgumentStep,
    ArtifactKind,
    BranchStatus,
    FailureHypothesis,
    LeanArtifact,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    SearchAction,
    SearchActionKind,
)
from ...proof_system.workspace.observation import (
    Observation,
    ObservationSource,
    observations_from_check_result,
)
from ..safety import SafetyVerdict
from .frontier import STALL_THRESHOLD, _stalled_streak
from .proposal import (
    AlignmentSpec,
    ArgumentStepSpec,
    DecomposeChildSpec,
)

if TYPE_CHECKING:
    from ...proof_system.workspace import ProofWorkspace

#: Consecutive same-goal failures before a REPAIR child branch is spawned.
#: Two identical-fingerprint failures suggest the realization is stuck but the
#: strategy may still be viable, so the search forks rather than abandons.
REPAIR_THRESHOLD = 2

_DECLARATION_ID_RE = re.compile(
    r"^[ \t]*(?:private[ \t]+)?(?:noncomputable[ \t]+)?"
    r"(?:theorem|lemma|def)[ \t]+([^\s:({]+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class StructuredActionResult:
    """Everything the reducer needs to fold one executed action's outcome."""

    branch_id: str
    check_result: CheckResult
    safety_verdict: SafetyVerdict
    proof_text: str
    source: str
    attempt_index: int


def apply(
    workspace: ProofWorkspace,
    action: SearchAction,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Return the workspace after folding ``result`` into ``action``'s branch.

    Never mutates ``workspace``; every change produces a successor (new
    ``version``). The caller's reference to the old workspace is untouched.
    """
    branch = _find_branch(workspace, result.branch_id)
    if branch is None:
        # The action targeted a branch that no longer exists (e.g. superseded
        # by a reducer transition we did not author). Drop the outcome
        # silently rather than corrupt the workspace.
        return workspace

    if action.kind is SearchActionKind.RUN_CAPABILITY_TEST:
        # Capability audits carry no proof body and run no safety review; they
        # only record an observation and, on a *missing* capability, block the
        # route (branch + obligation together). Branching before building an
        # artifact keeps the implement path unchanged.
        return _apply_capability_audit(workspace, branch, action, result)

    is_root = branch.obligation_id in workspace.root_obligation_ids
    artifact = LeanArtifact(
        source=result.source,
        obligation_id=branch.obligation_id,
        obligation_version=branch.obligation_version,
        proof_body=result.proof_text,
        declaration_id=None if is_root else _declaration_id(result.source),
        # A root obligation fills the task's proof hole (snippet); a helper
        # (decomposed child) is a standalone declaration the assembler emits as
        # its own top-level statement. The kind tells the assembler how to
        # render and the fact layer what to reuse as the established conclusion.
        kind=ArtifactKind.PROOF_BODY if is_root else ArtifactKind.DECLARATION,
    )

    if result.check_result.accepted and result.safety_verdict.accepted:
        return _accept(workspace, branch, action, artifact, result)

    return _record_failure(workspace, branch, action, artifact, result)


def apply_decompose(
    workspace: ProofWorkspace,
    action: SearchAction,
    *,
    children: Sequence[DecomposeChildSpec],
    parent_branch_id: str,
) -> ProofWorkspace:
    """Fold a ``DECOMPOSE`` action: split an obligation into helper children.

    A separate entry point from :func:`apply` because decomposition is a
    *structural* move, not the outcome of one executed proof: it carries no
    :class:`CheckResult`, no safety verdict, no artifact, and it spawns several
    new branches. Stuffing those into :class:`StructuredActionResult` would
    force every IMPLEMENT to pass ``None`` children, so the reducer keeps two
    doors.

    Steps, all immutable (every change is a successor):

    * resolve the parent branch and its current obligation; if the branch pins a
      *stale* obligation version (already superseded by an earlier decompose),
      return the workspace unchanged — re-decomposing a dead branch is a no-op;
    * build a :class:`ProofObligation` per child (``child.dependency_ids``
      narrowed to the sibling set so a child can only depend on co-declared
      siblings, never on the parent or external ids);
    * call :meth:`ProofWorkspace.decompose`, which inserts the children and
      supersedes the parent obligation with a new version depending on them;
    * retire every branch still pinning the *old* parent version to
      ``SUPERSEDED`` in the same successor — otherwise
      :meth:`ProofWorkspace.validate` flags "branch remains ACTIVE on a
      superseded obligation";
    * seed one ACTIVE branch for the new parent version and one per child.

    No observation, no artifact, no fact: decomposition proves nothing. The
    helpers become accepted facts only through a later accepted ``IMPLEMENT``,
    at which point the dependency-aware frontier lets the parent become ready.
    """
    if action.kind is not SearchActionKind.DECOMPOSE:
        # Defensive: only a DECOMPOSE action reaches here. A mismatched kind is
        # a controller bug; refuse to mutate rather than corrupt the workspace.
        return workspace
    branch = _find_branch(workspace, parent_branch_id)
    if branch is None:
        return workspace
    graph = workspace.obligation_graph
    current = graph.by_id(branch.obligation_id)
    if current is None or current.version != branch.obligation_version:
        # The branch pins a superseded obligation version; re-decomposing it
        # would target dead state. Drop the action.
        return workspace
    if not children:
        return workspace

    child_ids = [child.child_id for child in children]
    child_obligations: list[ProofObligation] = []
    for child in children:
        narrowed_deps = tuple(
            dep_id for dep_id in child.dependency_ids if dep_id in child_ids
        )
        child_obligations.append(
            ProofObligation(
                obligation_id=child.child_id,
                version=1,
                title=child.child_id,
                lean_statement=child.statement,
                dependency_ids=narrowed_deps,
                status=ObligationStatus.OPEN,
            )
        )

    parent_version_before = current.version
    workspace = workspace.decompose(branch.obligation_id, child_obligations)

    # Resolve the new parent version produced by decompose.
    new_parent = workspace.obligation_graph.by_id(branch.obligation_id)
    assert new_parent is not None and new_parent.version > parent_version_before

    retired_branches = tuple(
        replace(existing, status=BranchStatus.SUPERSEDED)
        if (
            existing.obligation_id == branch.obligation_id
            and existing.obligation_version == parent_version_before
        )
        else existing
        for existing in workspace.branches
    )

    parent_branch = replace(
        branch,
        # The new parent-version branch is a fresh strategy attempt on the
        # superseded obligation's successor, so it gets a new branch_id (a
        # branch_id must be unique within the workspace). ``.p<n>`` mirrors the
        # repair-child ``.r<n>`` convention and counts prior post-decompose
        # parent branches so the id is deterministic. It carries no last_action:
        # the DECOMPOSE action targeted the now-superseded old branch, and this
        # fresh branch has not yet had an IMPLEMENT run against it.
        branch_id=_next_parent_branch_id(branch.branch_id, workspace.branches),
        obligation_version=new_parent.version,
        lean_artifact=None,
        observations=(),
        status=BranchStatus.ACTIVE,
    )
    child_branches = tuple(
        ProofBranch(
            branch_id=f"{branch.branch_id}.d.{child_id}",
            obligation_id=child_id,
            obligation_version=1,
            parent_branch_id=branch.branch_id,
            status=BranchStatus.ACTIVE,
        )
        for child_id in child_ids
    )
    new_branches = (*retired_branches, parent_branch, *child_branches)
    return workspace.successor(branches=new_branches)


def apply_argument(
    workspace: ProofWorkspace,
    action: SearchAction,
    *,
    branch_id: str,
    new_steps: Sequence[ArgumentStepSpec] = (),
    new_alignments: Sequence[AlignmentSpec] = (),
    refined_steps: Sequence[ArgumentStepSpec] = (),
    refined_alignments: Sequence[AlignmentSpec] = (),
) -> ProofWorkspace:
    """Fold a ``PROPOSE_ARGUMENT`` / ``REFINE_ARGUMENT`` action in place.

    A separate entry point from :func:`apply` because editing the argument
    layer is a *structural* move: no :class:`CheckResult`, no safety verdict,
    no artifact. ``PROPOSE_ARGUMENT`` appends ``new_steps`` (with their
    ``new_alignments``) to the branch; ``REFINE_ARGUMENT`` substitutes each
    ``refined_steps[i]`` into the existing graph by ``step_id`` and replaces
    the matching ``refined_alignments`` by ``argument_step_id``.

    Every step landing on the branch must carry an alignment link — that is a
    hard :meth:`ProofBranch.validate` rule — so PROPOSE pairs each new step
    with its alignment in this one transition. To guarantee the workspace can
    never become invalid, the reducer builds the candidate branch, validates
    it, and only commits on a clean report; a malformed payload (missing
    alignment, an ``unaligned`` relation that supplies a Lean target, a REFINE
    that introduces a dependency cycle) is a no-op. REFINE of an unknown
    ``step_id`` is likewise a no-op rather than an error.
    """
    if action.kind not in (
        SearchActionKind.PROPOSE_ARGUMENT,
        SearchActionKind.REFINE_ARGUMENT,
    ):
        return workspace
    branch = _find_branch(workspace, branch_id)
    if branch is None or branch.status != BranchStatus.ACTIVE:
        return workspace

    if action.kind is SearchActionKind.PROPOSE_ARGUMENT:
        if not _alignments_cover(new_steps, new_alignments):
            return workspace
        if not _alignment_specs_valid(new_alignments):
            return workspace
        combined_steps = (
            *branch.argument.steps,
            *(_to_argument_step(spec) for spec in new_steps),
        )
        align_replacements = {}
        combined_alignments = (
            *branch.alignment,
            *(_to_alignment_link(spec) for spec in new_alignments),
        )
    else:  # REFINE_ARGUMENT
        if not _alignment_specs_valid(refined_alignments):
            return workspace
        existing_step_ids = {s.step_id for s in branch.argument.steps}
        existing_align_ids = {
            link.argument_step_id for link in branch.alignment
        }
        # A REFINE that names no existing step and no existing alignment has
        # nothing to replace — treat it as a no-op rather than producing a
        # successor that only advances ``last_action`` / the version counter.
        hits_steps = any(
            spec.step_id in existing_step_ids for spec in refined_steps
        )
        hits_alignments = any(
            spec.argument_step_id in existing_align_ids
            for spec in refined_alignments
        )
        if not hits_steps and not hits_alignments:
            return workspace
        replace_steps = {
            spec.step_id: _to_argument_step(spec) for spec in refined_steps
        }
        combined_steps = tuple(
            replace_steps.get(step.step_id, step)
            for step in branch.argument.steps
        )
        align_replacements = {
            spec.argument_step_id: _to_alignment_link(spec)
            for spec in refined_alignments
        }
        combined_alignments = tuple(
            align_replacements.get(link.argument_step_id, link)
            for link in branch.alignment
        )

    candidate = replace(
        branch,
        argument=ArgumentGraph(steps=combined_steps),
        alignment=combined_alignments,
        last_action=action,
    )
    if not candidate.validate().ok:
        return workspace
    return workspace.successor(
        branches=_replace_branch(workspace.branches, candidate)
    )


def apply_change_representation(
    workspace: ProofWorkspace,
    action: SearchAction,
    *,
    branch_id: str,
    argument_steps: Sequence[ArgumentStepSpec],
    alignments: Sequence[AlignmentSpec],
) -> ProofWorkspace:
    """Fold a ``CHANGE_REPRESENTATION`` action by forking a new branch.

    Structural — no check, no safety, no artifact. The new representation is a
    *full replacement* of the argument + alignment layers, so rather than edit
    the current branch the reducer forks ``<parent>.rep<n>`` carrying the
    payload's argument, inherits the parent's observations (so the child sees
    the evidence that motivated the strategy switch) and starts without a Lean
    artifact (a fresh realization comes from a later IMPLEMENT). The parent is
    retired to ``SUPERSEDED`` in the same successor so two incompatible
    argument layers never coexist as ACTIVE on one obligation. The child is
    pre-commit validated; a malformed payload is a no-op.
    """
    if action.kind is not SearchActionKind.CHANGE_REPRESENTATION:
        return workspace
    parent = _find_branch(workspace, branch_id)
    if parent is None or parent.status != BranchStatus.ACTIVE:
        return workspace
    if not _alignments_cover(argument_steps, alignments):
        return workspace
    if not _alignment_specs_valid(alignments):
        return workspace

    new_argument = ArgumentGraph(
        steps=tuple(_to_argument_step(spec) for spec in argument_steps)
    )
    new_alignment = tuple(_to_alignment_link(spec) for spec in alignments)
    child = ProofBranch(
        branch_id=_next_representation_branch_id(
            parent.branch_id, workspace.branches
        ),
        obligation_id=parent.obligation_id,
        obligation_version=parent.obligation_version,
        parent_branch_id=parent.branch_id,
        argument=new_argument,
        alignment=new_alignment,
        observations=parent.observations,
        lean_artifact=None,
        status=BranchStatus.ACTIVE,
    )
    if not child.validate().ok:
        return workspace

    retired_parent = replace(parent, status=BranchStatus.SUPERSEDED)
    new_branches = (*_replace_branch(workspace.branches, retired_parent), child)
    return workspace.successor(branches=new_branches)


def apply_failure_hypotheses(
    workspace: ProofWorkspace,
    *,
    branch_id: str,
    hypotheses: Sequence[FailureHypothesis],
) -> ProofWorkspace:
    """Append competing failure hypotheses to a branch, dropping invalid ones.

    A hypothesis is a model product (see ``hypothesis.py``): it competes with
    siblings rather than escalating up a blame hierarchy. The reducer never
    synthesizes hypotheses — it only validates the ones a generator produced
    against the branch's actual state and attaches the ones that resolve:

    * ``evidence_ids`` must all reference the branch's existing observations;
    * ``affected_step_ids`` must all reference the branch's argument steps;
    * each ``proposed_tests`` entry must target *this* branch;
    * ``hypothesis_id`` must not collide with an existing one.

    Any hypothesis failing a check is dropped silently rather than corrupting
    the workspace. If nothing survives, no successor is produced (the workspace
    is returned unchanged so the version counter does not advance on an empty
    edit). Hypotheses must be attached *after* the observations they cite are
    already on the branch — the controller enforces that ordering.
    """
    branch = _find_branch(workspace, branch_id)
    if branch is None:
        return workspace

    observation_ids = {obs.observation_id for obs in branch.observations}
    step_ids = {step.step_id for step in branch.argument.steps}
    existing_ids = {hyp.hypothesis_id for hyp in branch.failure_hypotheses}

    accepted: list[FailureHypothesis] = []
    for hypothesis in hypotheses:
        if not hypothesis.validate().ok:
            continue
        if hypothesis.hypothesis_id in existing_ids:
            continue
        if not set(hypothesis.evidence_ids) <= observation_ids:
            continue
        if hypothesis.affected_step_ids and not set(
            hypothesis.affected_step_ids
        ) <= step_ids:
            continue
        if any(
            test.target_branch_id != branch_id
            for test in hypothesis.proposed_tests
        ):
            continue
        existing_ids.add(hypothesis.hypothesis_id)
        accepted.append(hypothesis)

    if not accepted:
        return workspace
    updated = replace(
        branch,
        failure_hypotheses=(*branch.failure_hypotheses, *accepted),
    )
    return workspace.successor(
        branches=_replace_branch(workspace.branches, updated)
    )


def _next_parent_branch_id(
    parent_branch_id: str, branches: tuple[ProofBranch, ...]
) -> str:
    """Deterministic fresh branch_id for a post-decompose parent branch."""
    prefix = f"{parent_branch_id}.p"
    count = sum(1 for b in branches if b.branch_id.startswith(prefix))
    return f"{prefix}{count}"


def _next_representation_branch_id(
    parent_branch_id: str, branches: tuple[ProofBranch, ...]
) -> str:
    """Deterministic fresh branch_id for a representation child branch.

    Mirrors the ``.r<n>`` (repair) and ``.p<n>`` (post-decompose parent)
    conventions: count existing ``.rep<n>`` siblings so forks are
    deterministic. ``.rep`` is distinct from ``.r`` (repair) and ``.c``
    (candidate expansion), so the id space never collides.
    """
    prefix = f"{parent_branch_id}.rep"
    count = sum(1 for b in branches if b.branch_id.startswith(prefix))
    return f"{prefix}{count}"


def _to_argument_step(spec: ArgumentStepSpec) -> ArgumentStep:
    """Build a workspace :class:`ArgumentStep` from a payload spec."""
    return ArgumentStep(
        step_id=spec.step_id,
        claim=spec.claim,
        justification=spec.justification,
        depends_on=tuple(spec.depends_on),
        introduced_fact_ids=tuple(spec.introduced_fact_ids),
        confidence=spec.confidence,
    )


def _to_alignment_link(spec: AlignmentSpec) -> AlignmentLink:
    """Build a workspace :class:`AlignmentLink` from a payload spec.

    Enforces the two ``ProofBranch.validate`` alignment rules up front:
    ``unaligned`` relations carry no Lean target (the three target fields are
    forced to ``None``), and ``implements`` / ``partial`` relations carry at
    least one target. A spec violating the second rule still builds a link, but
    the reducer's pre-commit branch validation will reject it (no-op); forcing
    ``unaligned`` targets to ``None`` here keeps the common ``unaligned`` case
    from ever reaching that guard.
    """
    relation = AlignmentRelation(spec.relation)
    if relation is AlignmentRelation.UNALIGNED:
        return AlignmentLink(argument_step_id=spec.argument_step_id, relation=relation)
    return AlignmentLink(
        argument_step_id=spec.argument_step_id,
        lean_declaration_id=spec.lean_declaration_id,
        goal_fingerprint=spec.goal_fingerprint,
        source_span=spec.source_span,
        relation=relation,
    )


def _alignments_cover(
    steps: Sequence[ArgumentStepSpec], alignments: Sequence[AlignmentSpec]
) -> bool:
    """True iff every ``steps[i].step_id`` has exactly one matching alignment.

    The reducer refuses a PROPOSE whose steps lack full alignment coverage
    rather than emitting a branch that fails the "every step has an alignment"
    rule. Duplicate alignment entries for one step also fail coverage.
    """
    covered: set[str] = set()
    for spec in alignments:
        if spec.argument_step_id in covered:
            return False
        covered.add(spec.argument_step_id)
    return {step.step_id for step in steps} == covered


def _alignment_specs_valid(alignments: Sequence[AlignmentSpec]) -> bool:
    for spec in alignments:
        try:
            relation = AlignmentRelation(spec.relation)
        except ValueError:
            return False
        has_target = any(
            target is not None
            for target in (
                spec.lean_declaration_id,
                spec.goal_fingerprint,
                spec.source_span,
            )
        )
        if relation is AlignmentRelation.UNALIGNED and has_target:
            return False
        if relation is not AlignmentRelation.UNALIGNED and not has_target:
            return False
    return True


def _declaration_id(source: str) -> str | None:
    """Best-effort Lean declaration name from a standalone helper artifact."""
    match = _DECLARATION_ID_RE.search(source)
    if match is None:
        return None
    return match.group(1)


def _accept(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    action: SearchAction,
    artifact: LeanArtifact,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Mark the branch ACCEPTED and register the obligation as a verified fact.

    The fact's ``statement`` is the artifact's rendered source — what a
    dependent obligation reuses as the established conclusion. For a root the
    source is the proof body (baseline behaviour); for a helper it is the
    helper's full declaration (so the parent prompt can reuse it by name). The
    artifact kind already encodes that distinction; the fact layer just mirrors
    the rendered text.
    """
    accepted_branch = replace(
        branch,
        lean_artifact=artifact,
        last_action=action,
        status=BranchStatus.ACCEPTED,
    )
    new_branches = _replace_branch(workspace.branches, accepted_branch)
    workspace = workspace.successor(branches=new_branches)
    return workspace.register_accepted_fact(
        branch.obligation_id,
        statement=artifact.source,
        source_attempt_index=result.attempt_index,
        check_result=result.check_result,
        safety_accepted=True,
        declaration_id=artifact.declaration_id,
        artifact_source=artifact.source,
    )


def _record_failure(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    action: SearchAction,
    artifact: LeanArtifact,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Append evidence to an ACTIVE branch, retiring or forking it if stalled."""
    new_observations = _observations_for(result)
    updated_branch = replace(
        branch,
        # Retain the artifact as provenance: a failed realization does not
        # negate its mathematical strategy, and the trace should keep it.
        lean_artifact=artifact,
        last_action=action,
        observations=(*branch.observations, *new_observations),
    )

    stalled = _stalled_streak(updated_branch)
    if stalled >= STALL_THRESHOLD:
        updated_branch = replace(updated_branch, status=BranchStatus.DORMANT)

    new_branches = _replace_branch(workspace.branches, updated_branch)
    if _should_spawn_repair_child(updated_branch, action, new_branches):
        new_branches = (*new_branches, _make_repair_child(updated_branch, new_branches))

    return workspace.successor(branches=new_branches)


#: Checker categories that signal a *capability the environment lacks* — the
#: route cannot be pursued until the environment changes. A capability audit is
#: the one place the reducer is allowed to block a route: a missing capability
#: is an environmental fact, not a judgment about the proposition.
_CAPABILITY_MISSING_CATEGORIES: frozenset[DiagnosticCategory] = frozenset(
    {
        DiagnosticCategory.UNKNOWN_IDENTIFIER,
        DiagnosticCategory.INVALID_REFERENCE,
        DiagnosticCategory.TOOL_UNAVAILABLE,
    }
)


def _capability_missing(check_result: CheckResult) -> bool:
    """True iff the checker reports an environment the route cannot supply.

    Only ``UNKNOWN_IDENTIFIER`` / ``INVALID_REFERENCE`` / ``TOOL_UNAVAILABLE``
    block: they name a resource (tactic, lemma, import) the proof needs but the
    environment does not have. Other failures (unsolved goals, type mismatch) are
    implementation problems a later IMPLEMENT may still solve, so the audit must
    not block on them — capability audits only block routes, never propositions.
    """
    return check_result.category in _CAPABILITY_MISSING_CATEGORIES


def _apply_capability_audit(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    action: SearchAction,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Fold a capability-audit outcome into the branch (and maybe obligation).

    Always appends a neutral ``CAPABILITY_AUDIT`` observation so the probe is
    never silently dropped. When the checker reports the capability as *missing*
    (see :func:`_capability_missing`) the route is blocked: the branch goes
    ``BLOCKED`` and the obligation goes ``BLOCKED`` in the same successor, so
    ``ResultSummary.blocked_branch_obligation_ids`` collapses to empty and the
    frontier drops the branch via its non-ACTIVE status. Otherwise the branch
    stays ACTIVE with the audit recorded as evidence and the search continues.
    """
    observation = _capability_observation(result)
    updated_branch = replace(
        branch,
        last_action=action,
        observations=(*branch.observations, observation),
    )

    if not _capability_missing(result.check_result):
        new_branches = _replace_branch(workspace.branches, updated_branch)
        return workspace.successor(branches=new_branches)

    blocked_branch = replace(updated_branch, status=BranchStatus.BLOCKED)
    new_branches = _replace_branch(workspace.branches, blocked_branch)
    workspace = workspace.successor(branches=new_branches)
    return _block_obligation(workspace, branch.obligation_id)


def _capability_observation(result: StructuredActionResult) -> Observation:
    evidence_ref = f"capability:{result.attempt_index}"
    check = result.check_result
    feedback = check.parsed_feedback
    message = (
        feedback.message
        if feedback is not None and feedback.message
        else (check.raw_output.strip()[:160] if check.raw_output else "")
    )
    prefix = "capability available" if check.accepted else "capability probe failed"
    if message:
        message = f"{prefix}: {message}"
    else:
        message = prefix
    return Observation(
        observation_id=f"{evidence_ref}:capability",
        source=ObservationSource.CAPABILITY_AUDIT,
        category=check.category.value,
        message=message,
        raw_evidence_ref=evidence_ref,
    )


def _block_obligation(
    workspace: ProofWorkspace, obligation_id: str
) -> ProofWorkspace:
    """Flip an obligation to ``BLOCKED`` immutably (mirror of accept/register)."""
    graph = workspace.obligation_graph
    obligation = graph.by_id(obligation_id)
    if obligation is None:
        return workspace
    blocked = replace(obligation, status=ObligationStatus.BLOCKED)
    new_graph = graph.with_obligation(blocked)
    return workspace.successor(obligation_graph=new_graph)


def _observations_for(result: StructuredActionResult) -> tuple[Observation, ...]:
    """Neutral observations for a failure, plus a safety note if relevant."""
    observations = list(observations_from_check_result(
        result.check_result, result.attempt_index
    ))
    if result.check_result.accepted and not result.safety_verdict.accepted:
        observations.append(_safety_observation(result))
    return tuple(observations)


def _safety_observation(result: StructuredActionResult) -> Observation:
    evidence_ref = f"attempt:{result.attempt_index}"
    return Observation(
        observation_id=f"{evidence_ref}:safety",
        source=ObservationSource.CHECKER,
        category="safety_rejected",
        message="; ".join(result.safety_verdict.reasons) or "safety review rejected",
        raw_evidence_ref=evidence_ref,
    )


def _should_spawn_repair_child(
    branch: ProofBranch,
    action: SearchAction,
    branches: tuple[ProofBranch, ...],
) -> bool:
    """Spawn a REPAIR child when a root strategy branch stalls repeatedly.

    Forking is bounded: only a branch that is itself a root strategy attempt
    (no ``parent_branch_id``) and has not already spawned a repair child may
    fork. This keeps the branch tree shallow — a stalled repair child retires
    to DORMANT via the stall threshold rather than spawning nested siblings —
    while still giving the search one fresh realization attempt per stalled
    strategy. The action kind (IMPLEMENT vs REPAIR_IMPLEMENTATION) is
    irrelevant: what matters is that the realization keeps failing on the same
    goals.
    """
    del action  # fork rule is stall-driven, not action-kind-driven
    if branch.parent_branch_id is not None:
        return False
    if branch.status == BranchStatus.ACCEPTED:
        return False
    if _stalled_streak(branch) < REPAIR_THRESHOLD:
        return False
    prefix = f"{branch.branch_id}.r"
    if any(sibling.branch_id.startswith(prefix) for sibling in branches):
        return False
    return True


def _make_repair_child(
    parent: ProofBranch, branches: tuple[ProofBranch, ...]
) -> ProofBranch:
    """Derive a REPAIR child branch from a stalled parent.

    Inherits the argument, alignment, and accumulated observations (so the
    child sees the failure evidence that motivated the fork) but starts
    without a Lean artifact — the next IMPLEMENT will supply a fresh
    realization. A new ``branch_id`` (``<parent>.r<n>``) guarantees the parent
    is never overwritten; ``n`` counts existing repair siblings so forks are
    deterministic.
    """
    prefix = f"{parent.branch_id}.r"
    child_index = sum(
        1 for branch in branches if branch.branch_id.startswith(prefix)
    )
    return ProofBranch(
        branch_id=f"{parent.branch_id}.r{child_index}",
        obligation_id=parent.obligation_id,
        obligation_version=parent.obligation_version,
        parent_branch_id=parent.branch_id,
        argument=parent.argument,
        alignment=parent.alignment,
        observations=parent.observations,
        lean_artifact=None,
        status=BranchStatus.ACTIVE,
    )


def _replace_branch(
    branches: tuple[ProofBranch, ...], updated: ProofBranch
) -> tuple[ProofBranch, ...]:
    """Return a copy of ``branches`` with ``updated`` substituted in place."""
    replaced = tuple(
        updated if branch.branch_id == updated.branch_id else branch
        for branch in branches
    )
    if not any(branch.branch_id == updated.branch_id for branch in replaced):
        raise KeyError(
            f"branch {updated.branch_id!r} not present in workspace branches"
        )
    return replaced


def _find_branch(
    workspace: ProofWorkspace, branch_id: str
) -> ProofBranch | None:
    for branch in workspace.branches:
        if branch.branch_id == branch_id:
            return branch
    return None
