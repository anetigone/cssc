"""Mutable frontier scheduler for structured search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.proof_system.workspace import BranchStatus, ObligationStatus

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofBranch, ProofWorkspace


STALL_THRESHOLD = 3


@dataclass(frozen=True)
class FrontierNode:
    """One schedulable branch/obligation pair."""

    branch_id: str
    obligation_id: str
    depth_from_root: int
    attempt_count: int
    last_goal_fingerprints: tuple[str, ...]
    stalled_streak: int


def _branch_goal_fingerprints(branch: ProofBranch) -> tuple[str, ...]:
    """Fingerprint set of the branch's most recent checker observation batch."""
    if not branch.observations:
        return ()
    latest_ref = branch.observations[-1].raw_evidence_ref
    latest = [
        obs
        for obs in branch.observations
        if obs.raw_evidence_ref == latest_ref and obs.goal_fingerprint
    ]
    if not latest:
        latest = [branch.observations[-1]]
    fingerprints = sorted(
        obs.goal_fingerprint for obs in latest if obs.goal_fingerprint
    )
    return tuple(fingerprints)


def _depth_from_root(branch: ProofBranch, workspace: ProofWorkspace) -> int:
    """Count parent links from this branch back to a root branch."""
    by_id = {b.branch_id: b for b in workspace.branches}
    depth = 0
    current_id: str | None = branch.parent_branch_id
    seen: set[str] = set()
    while current_id is not None and current_id in by_id and current_id not in seen:
        seen.add(current_id)
        depth += 1
        current_id = by_id[current_id].parent_branch_id
    return depth


def _node_for(branch: ProofBranch, workspace: ProofWorkspace) -> FrontierNode:
    return FrontierNode(
        branch_id=branch.branch_id,
        obligation_id=branch.obligation_id,
        depth_from_root=_depth_from_root(branch, workspace),
        attempt_count=_attempt_count(branch),
        last_goal_fingerprints=_branch_goal_fingerprints(branch),
        stalled_streak=_stalled_streak(branch),
    )


_SOLVABLE_STATUSES: frozenset[ObligationStatus] = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)


def _is_ready(branch: ProofBranch, workspace: ProofWorkspace) -> bool:
    """True iff ``branch``'s obligation can be worked now."""
    if branch.status != BranchStatus.ACTIVE:
        return False
    obligation = workspace.obligation_graph.by_id(branch.obligation_id)
    if obligation is None or obligation.status not in _SOLVABLE_STATUSES:
        return False
    graph = workspace.obligation_graph
    for dependency_id in obligation.dependency_ids:
        dependency = graph.by_id(dependency_id)
        if dependency is None or dependency.status != ObligationStatus.ACCEPTED:
            return False
    return True


def _attempt_count(branch: ProofBranch) -> int:
    """Number of distinct attempt evidence refs on this branch."""
    refs = {
        obs.raw_evidence_ref
        for obs in branch.observations
        if obs.raw_evidence_ref
    }
    return len(refs)


def _stalled_streak(branch: ProofBranch) -> int:
    """Number of trailing attempts stuck on the same goal-fingerprint set."""
    observations = branch.observations
    if not observations:
        return 0
    batches: list[tuple[str, ...]] = []
    seen_refs: set[str] = set()
    for obs in reversed(observations):
        if not obs.raw_evidence_ref:
            continue
        if obs.raw_evidence_ref in seen_refs:
            continue
        seen_refs.add(obs.raw_evidence_ref)
        batch = tuple(
            sorted(
                inner.goal_fingerprint
                for inner in observations
                if inner.raw_evidence_ref == obs.raw_evidence_ref
                and inner.goal_fingerprint
            )
        )
        batches.append(batch)
    if not batches or batches[0] == ():
        return 0
    streak = 1
    for current, previous in zip(batches, batches[1:]):
        if previous == batches[0]:
            streak += 1
        else:
            break
    return streak


class Frontier:
    """Mutable scheduler for ready branches."""

    def __init__(self) -> None:
        self._pending: list[FrontierNode] = []
        self._pending_ids: set[str] = set()
        self._popped_this_round: set[str] = set()

    def seed(self, workspace: ProofWorkspace) -> None:
        """Load all ready branches of the workspace as pending nodes."""
        self._pending = []
        self._pending_ids = set()
        self._popped_this_round = set()
        for branch in workspace.branches:
            if _is_ready(branch, workspace):
                node = _node_for(branch, workspace)
                self._pending.append(node)
                self._pending_ids.add(node.branch_id)

    def has_work(self) -> bool:
        """True iff at least one pending node remains."""
        return bool(self._pending)

    def pop(self) -> FrontierNode:
        """Return and remove the highest-priority pending node."""
        if not self._pending:
            raise StopIteration("frontier is empty")
        self._pending.sort(key=_priority_key)
        node = self._pending.pop(0)
        self._pending_ids.discard(node.branch_id)
        self._popped_this_round.add(node.branch_id)
        return node

    def update(
        self,
        workspace: ProofWorkspace,
        branch_id: str,
        accepted: bool,
        *,
        attempted_branch_ids: tuple[str, ...] = (),
    ) -> None:
        """Refresh the pending set after a reducer transition."""
        del branch_id, accepted  # status in the workspace is authoritative
        self._popped_this_round.update(attempted_branch_ids)

        ready = [
            branch
            for branch in workspace.branches
            if _is_ready(branch, workspace)
        ]
        eligible = [
            branch
            for branch in ready
            if branch.branch_id not in self._popped_this_round
        ]
        if not eligible and ready:
            self._popped_this_round.clear()
            eligible = ready

        fresh = [_node_for(branch, workspace) for branch in eligible]
        fresh_ids = {node.branch_id for node in fresh}
        self._pending = fresh
        self._pending_ids = fresh_ids


def _priority_key(node: FrontierNode) -> tuple[int, int, int, str]:
    """Stable sort key."""
    return (
        node.stalled_streak,
        node.depth_from_root,
        node.attempt_count,
        node.branch_id,
    )
