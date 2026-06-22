"""Mutable frontier scheduler for the structured AND-OR search.

The frontier is the one mutable object in the structured search: ``pop`` has a
side effect (a branch leaves the pending set). It is *only* a scheduler,
though. It never mutates the :class:`ProofWorkspace` — all workspace change
flows through the :class:`StructuredReducer`. The frontier reads workspace
state (which branches are still ACTIVE, their latest observations) and decides
which (branch, obligation) pair the controller should try next.

Selection is deterministic so a run replays from its trace: a stable tuple
sort over ``(depth_from_root, stalled_streak, attempt_count, branch_id)``.
There is no MCTS or learned score in this first version (``tmp/plan1.md`` §12).

Stall detection lives here because it drives scheduling: a branch that keeps
hitting the same goal fingerprint set with no progress is bumped down the
order and, once it crosses :data:`STALL_THRESHOLD`, the reducer retires it to
DORMANT (the frontier then drops it from the pending set on the next
``update``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...proof_system.workspace import BranchStatus

if TYPE_CHECKING:
    from ...proof_system.workspace import ProofBranch, ProofWorkspace


#: Consecutive identical goal-fingerprint sets before a branch is retired.
#: A branch stuck on the same goals this many attempts is unlikely to escape
#: without a strategy change, so the reducer moves it to DORMANT and the
#: frontier stops re-queueing it.
STALL_THRESHOLD = 3


@dataclass(frozen=True)
class FrontierNode:
    """One schedulable (branch, obligation) pair.

    Phase 6 only ever has the single root obligation, so ``obligation_id`` is
    constant; the field is kept so the scheduler stays obligation-aware when
    Phase 7 adds decomposition.
    """

    branch_id: str
    obligation_id: str
    depth_from_root: int
    attempt_count: int
    last_goal_fingerprints: tuple[str, ...]
    stalled_streak: int


def _branch_goal_fingerprints(branch: ProofBranch) -> tuple[str, ...]:
    """Fingerprint set of a branch's most recent checker observations.

    Observations are append-only and ordered newest-last, so the last batch
    sharing the same ``raw_evidence_ref`` is the latest attempt's evidence. We
    take the non-None goal fingerprints of that batch, sorted for stability —
    order within one attempt is irrelevant to "did the goal state change?".
    """
    if not branch.observations:
        return ()
    latest_ref = branch.observations[-1].raw_evidence_ref
    latest = [
        obs
        for obs in branch.observations
        if obs.raw_evidence_ref == latest_ref and obs.goal_fingerprint
    ]
    if not latest:
        # Fall back to the single most recent observation set even when it has
        # no goal fingerprint (e.g. a summary observation): an empty signature
        # still participates in stall detection because two such attempts are
        # indistinguishable.
        latest = [branch.observations[-1]]
    fingerprints = sorted(
        obs.goal_fingerprint for obs in latest if obs.goal_fingerprint
    )
    return tuple(fingerprints)


def _depth_from_root(branch: ProofBranch, workspace: ProofWorkspace) -> int:
    """Count parent links from this branch back to a root branch.

    The branch parent chain (``parent_branch_id``) records repair / strategy
    descent, not obligation dependency, so depth here is "how many strategy
    hops from a root attempt". Used only as a scheduling tiebreaker.
    """
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


def _attempt_count(branch: ProofBranch) -> int:
    """Number of distinct attempts recorded against this branch.

    Observations are append-only and one attempt contributes one
    ``raw_evidence_ref`` (``attempt:<N>``), so the count of distinct evidence
    refs is the attempt count. A branch that produced no observations yet has
    been attempted zero times from the frontier's perspective.
    """
    refs = {
        obs.raw_evidence_ref
        for obs in branch.observations
        if obs.raw_evidence_ref
    }
    return len(refs)


def _stalled_streak(branch: ProofBranch) -> int:
    """Number of trailing attempts stuck on the same goal-fingerprint set.

    Walks the observations newest-first, grouping by ``raw_evidence_ref`` to
    recover per-attempt evidence batches, and counts how many trailing
    attempts share the same non-empty fingerprint signature — *including* the
    most recent attempt. So one failing attempt on a goal is a streak of 1,
    three identical failures is a streak of 3. Deterministic and pure — the
    reducer recomputes the same value when deciding whether to retire or fork
    a branch, so the scheduler and the reducer never disagree.

    A streak of 0 means the branch either has no observations or its latest
    attempt differs from the one before it (progress, or first attempt).
    """
    observations = branch.observations
    if not observations:
        return 0
    # Group observations into per-attempt batches preserving newest-first order.
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
        # Latest attempt produced no fingerprintable goals: not comparable,
        # treat as a fresh streak so we don't retire on unparseable output.
        return 0
    streak = 1
    for current, previous in zip(batches, batches[1:]):
        if previous == batches[0]:
            streak += 1
        else:
            break
    return streak


class Frontier:
    """Mutable scheduler that picks the next (branch, obligation) to try.

    ``seed`` loads the initial ACTIVE branches. ``pop`` returns the best
    pending node (stable tuple sort) and marks it as popped this round so it
    is not re-queued until ``update`` runs with the reducer's new workspace.
    ``update`` refreshes the pending set from the workspace's current ACTIVE
    branches and drops anything retired (DORMANT / SUPERSEDED / BLOCKED /
    ACCEPTED).
    """

    def __init__(self) -> None:
        self._pending: list[FrontierNode] = []
        self._pending_ids: set[str] = set()
        self._popped_this_round: set[str] = set()

    def seed(self, workspace: ProofWorkspace) -> None:
        """Load all ACTIVE branches of the workspace as pending nodes."""
        self._pending = []
        self._pending_ids = set()
        self._popped_this_round = set()
        for branch in workspace.branches:
            if branch.status == BranchStatus.ACTIVE:
                node = _node_for(branch, workspace)
                self._pending.append(node)
                self._pending_ids.add(node.branch_id)

    def has_work(self) -> bool:
        """True iff at least one pending node remains to try this round."""
        return bool(self._pending)

    def pop(self) -> FrontierNode:
        """Return and remove the highest-priority pending node.

        Raises ``StopIteration`` if empty (callers gate on ``has_work`` first).
        Selection is deterministic: stable sort over the priority tuple.
        """
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
    ) -> None:
        """Refresh the pending set after a reducer transition.

        Rebuilds ``_pending`` from the workspace's current ACTIVE branches,
        excluding any already popped this round. Retired branches are dropped
        naturally (they are no longer ACTIVE). Resets the per-round popped set
        so the just-tried branch can be retried next round if it stayed ACTIVE.
        """
        del accepted  # accepted branches are no longer ACTIVE, so they drop out
        self._popped_this_round.discard(branch_id)
        fresh: list[FrontierNode] = []
        fresh_ids: set[str] = set()
        for branch in workspace.branches:
            if branch.status != BranchStatus.ACTIVE:
                continue
            if branch.branch_id in self._popped_this_round:
                fresh.append(_node_for(branch, workspace))
                fresh_ids.add(branch.branch_id)
                continue
            fresh.append(_node_for(branch, workspace))
            fresh_ids.add(branch.branch_id)
        self._pending = fresh
        self._pending_ids = fresh_ids


def _priority_key(node: FrontierNode) -> tuple[int, int, int, str]:
    """Stable sort key: fewer stalls, shallower depth, fewer attempts first.

    Stall is the dominant signal: a branch repeating the same goal fingerprint
    is unlikely to escape without a strategy change, so it must defer to every
    non-stalled branch regardless of how shallow it is. Within the same stall
    bucket, shallower branches (closer to a root attempt) and less-explored
    branches (fewer attempts) go first to keep the search breadth-favoring.
    """
    return (
        node.stalled_streak,
        node.depth_from_root,
        node.attempt_count,
        node.branch_id,
    )
