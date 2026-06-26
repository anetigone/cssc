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

Phase 7.7 sharpens two workspace-level consequences of these transitions:

* a blocked helper now propagates transitively — every active obligation whose
  dependency closure contains a blocked obligation is flipped to BLOCKED in the
  same successor, so a dead helper makes its parents dead too rather than
  leaving them reported as still-open;
* DORMANT is no longer terminal. When new evidence lands (a dependency fact is
  accepted, or a capability audit reports a usable resource) the stalled
  branches that evidence may unstick are revived to ACTIVE. This is
  evidence-driven, not frontier-driven: the frontier re-queues a revived branch
  via its ACTIVE status, and an identical re-stall retires it again, so revival
  cannot loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agent.proof_system.base import CheckResult, DiagnosticCategory
from agent.proof_system.workspace import (
    ArtifactKind,
    BranchStatus,
    LeanArtifact,
    ObligationGraph,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    SearchAction,
    SearchActionKind,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
    observations_from_check_result,
)
from agent.search.safety import SafetyVerdict
from ..frontier import STALL_THRESHOLD, _stalled_streak
from ..proposal import DecomposeChildSpec
from .decompose import apply_decompose
from .structural import (
    apply_argument,
    apply_change_representation,
    apply_failure_hypotheses,
)

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofWorkspace

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
    workspace = workspace.register_accepted_fact(
        branch.obligation_id,
        statement=artifact.source,
        source_attempt_index=result.attempt_index,
        check_result=result.check_result,
        safety_accepted=True,
        declaration_id=artifact.declaration_id,
        artifact_source=artifact.source,
    )
    # Evidence-driven DORMANT recovery: a newly-verified fact is the strongest
    # signal that a stalled branch depending on this obligation may now succeed.
    # Revive matching DORMANT branches so the frontier re-queues them. No-op
    # when nothing is dormant (the single-root baseline always takes this path
    # and returns the workspace unchanged).
    return _reactivate_dormant(
        workspace, trigger_obligation_id=branch.obligation_id
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
        workspace = workspace.successor(branches=new_branches)
        # A capability the audit reports as *available* is new evidence: a
        # branch on the same obligation that had stalled for lack of it may now
        # make progress. Revive such DORMANT branches (no-op if there are none).
        return _reactivate_dormant(
            workspace, trigger_obligation_id=branch.obligation_id
        )

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
    """Block an obligation and propagate the block to every dependent.

    A helper going ``BLOCKED`` is a mechanical dead-end the search cannot
    escape, and an obligation that depends on it can never be proved either.
    So the block propagates transitively up the reverse dependency edges: any
    active obligation whose dependency closure now contains a blocked
    obligation is flipped to ``BLOCKED`` in the *same* successor. ``block_branch``
    (the ``no_actions`` path) is intentionally not routed through here — it only
    retires a branch, leaving the obligation open, because a generator producing
    no candidates is a different gap than a missing capability.

    This keeps the workspace status honest: a blocked helper makes its parents
    blocked too, so the run finalizer can classify the run as ``BLOCKED`` rather
    than reporting dead parents as still-open work.
    """
    graph = workspace.obligation_graph
    obligation = graph.by_id(obligation_id)
    if obligation is None:
        return workspace
    # Reverse adjacency: for each dependency, who depends on it? Edges in the
    # graph run obligation -> dependency (parent -> helper), so the reverse map
    # walks from a blocked helper back to every parent that needs it.
    dependents: dict[str, list[str]] = {}
    for active in graph.active():
        for dependency_id in active.dependency_ids:
            dependents.setdefault(dependency_id, []).append(
                active.obligation_id
            )
    closure: set[str] = set()
    frontier = [obligation_id]
    while frontier:
        current = frontier.pop()
        if current in closure:
            continue
        closure.add(current)
        frontier.extend(dependents.get(current, ()))
    new_graph = graph
    for blocked_id in closure:
        current = new_graph.by_id(blocked_id)
        if current is None:
            continue
        if current.status not in (
            ObligationStatus.OPEN,
            ObligationStatus.IN_PROGRESS,
        ):
            # Leave already-terminal obligations (ACCEPTED / BLOCKED /
            # SUPERSEDED) untouched: a verified fact stays verified even if a
            # sibling route dies, and superseded versions are provenance.
            continue
        new_graph = new_graph.with_obligation(
            replace(current, status=ObligationStatus.BLOCKED)
        )
    return workspace.successor(obligation_graph=new_graph)


def _reactivate_dormant(
    workspace: ProofWorkspace, *, trigger_obligation_id: str
) -> ProofWorkspace:
    """Revive DORMANT branches a new piece of evidence may unstick.

    DORMANT is not terminal — it means a branch stalled on the same goals and
    was set aside. When new evidence arrives (a dependency fact accepted, a
    capability audit finding a usable resource), a previously-stalled branch on
    the *same* obligation — or on an obligation that depends on the trigger —
    deserves another attempt: the reason it stalled may now be resolved.

    This is evidence-driven, not frontier-driven: it runs only at the moment a
    fact is registered or a capability clears, never because the frontier ran
    empty (that would just re-burn budget on the same stuck goals). Revival
    flips the branch back to ``ACTIVE`` and touches nothing else — the frontier
    re-queues it via its ``ACTIVE`` status on the next ``update``. If the next
    attempt still hits identical goals, the stall counter resumes and the branch
    goes DORMANT again, so revival cannot create an infinite loop.
    """
    graph = workspace.obligation_graph
    # Branches eligible for revival: those pinning the trigger obligation, or
    # any obligation whose dependency closure contains the trigger (a parent
    # whose helper just got verified). Resolved by id so superseded versions do
    # not falsely match.
    dependent_ids: set[str] = {trigger_obligation_id}
    for active in graph.active():
        reachable = _dependency_closure(active.obligation_id, graph)
        if trigger_obligation_id in reachable:
            dependent_ids.add(active.obligation_id)
    revived = False
    new_branches = list(workspace.branches)
    for index, branch in enumerate(new_branches):
        if (
            branch.status == BranchStatus.DORMANT
            and branch.obligation_id in dependent_ids
        ):
            new_branches[index] = replace(branch, status=BranchStatus.ACTIVE)
            revived = True
    if not revived:
        return workspace
    return workspace.successor(branches=tuple(new_branches))


def _dependency_closure(
    obligation_id: str, graph: ObligationGraph
) -> set[str]:
    """Obligation ids reachable from ``obligation_id`` along dependency edges."""
    visited: set[str] = set()
    frontier = [obligation_id]
    while frontier:
        current_id = frontier.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        current = graph.by_id(current_id)
        if current is None:
            continue
        frontier.extend(current.dependency_ids)
    return visited


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
