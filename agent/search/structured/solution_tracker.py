"""Solution selection helpers for structured search."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.proof_system.workspace import BranchStatus
from agent.proof_system.workspace.obligation import ObligationStatus

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofBranch, ProofWorkspace


def _accepted_branches_for(
    workspace: ProofWorkspace, obligation_id: str
) -> tuple[ProofBranch, ...]:
    """ACCEPTED branches whose obligation pin matches the current version."""
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
    """True iff every active obligation has a compatible accepted branch."""
    report = workspace.validate()
    if not report.ok:
        return False
    for obligation in workspace.obligation_graph.active():
        if not _accepted_branches_for(workspace, obligation.obligation_id):
            return False
    return True


def select_solution(workspace: ProofWorkspace) -> tuple[ProofBranch, ...]:
    """Pick one accepted branch per active obligation, dependency-first."""
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
