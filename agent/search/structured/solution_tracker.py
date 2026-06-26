"""SolutionTracker: decide when the structured search has a complete proof.

A "complete solution" is a set of branches — one per active obligation — that
are mutually version-compatible, all :attr:`BranchStatus.ACCEPTED`, and each
carries a :class:`LeanArtifact`. The tracker never mutates state; it only
reads the workspace and reports facts. The controller calls
:func:`has_complete_solution` to decide whether to invoke the assembler and
:func:`select_solution` to extract the artifacts for the final whole-source
recheck.

Phase 6 only exercises the single-root case, but the logic is obligation-aware
so decomposition (Phase 7) needs no change here: the rule is "every active
obligation in the DAG has a compatible accepted branch with an artifact".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.proof_system.workspace import BranchStatus
from agent.proof_system.workspace.obligation import ObligationStatus

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofBranch, ProofWorkspace


def _accepted_branches_for(
    workspace: ProofWorkspace, obligation_id: str
) -> tuple[ProofBranch, ...]:
    """ACCEPTED branches whose obligation pin matches the current version.

    A branch pins ``(obligation_id, obligation_version)``. It counts as a
    solution candidate for ``obligation_id`` only if its pinned version equals
    the obligation's current (latest non-superseded) version, so a stale
    accepted branch from a superseded obligation revision is never selected.
    Branches without a Lean artifact are excluded: assembly needs a source to
    concatenate.
    """
    current = workspace.obligation_graph.by_id(obligation_id)
    if current is None:
        return ()
    candidates = [
        branch
        for branch in workspace.branches
        if branch.obligation_id == obligation_id
        and branch.obligation_version == current.version
        and branch.status == BranchStatus.ACCEPTED
        and branch.lean_artifact is not None
    ]
    return tuple(sorted(candidates, key=lambda branch: branch.branch_id))


def has_complete_solution(workspace: ProofWorkspace) -> bool:
    """True iff every active obligation has a compatible accepted branch.

    An obligation with no accepted branch, or only stale/stale-version ones,
    blocks the solution. This mirrors :meth:`ArtifactAssembler.assemble`'s
    precondition (all active obligations ACCEPTED, each with a matching
    artifact) so the tracker and the assembler never disagree on readiness.
    """
    report = workspace.validate()
    if not report.ok:
        return False
    for obligation in workspace.obligation_graph.active():
        if not _accepted_branches_for(workspace, obligation.obligation_id):
            return False
    return True


def select_solution(workspace: ProofWorkspace) -> tuple[ProofBranch, ...]:
    """Pick one accepted branch per active obligation, dependency-first.

    For each active obligation the smallest-``branch_id`` compatible accepted
    branch is chosen (deterministic). The result is ordered so dependencies
    precede dependents, matching the assembler's concatenation order.
    """
    active = list(workspace.obligation_graph.active())
    by_id = {obligation.obligation_id: obligation for obligation in active}
    selected: list[ProofBranch] = []
    emitted: set[str] = set()

    def emit(obligation_id: str) -> None:
        if obligation_id in emitted or obligation_id not in by_id:
            return
        obligation = by_id[obligation_id]
        for dependency_id in obligation.dependency_ids:
            emit(dependency_id)
        if obligation_id in emitted:
            return
        candidates = _accepted_branches_for(workspace, obligation_id)
        if candidates:
            selected.append(candidates[0])
        emitted.add(obligation_id)

    for obligation in active:
        emit(obligation.obligation_id)
    return tuple(selected)
