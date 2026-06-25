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

Phase 7.4 adds a *readiness gate*: an obligation whose dependencies are not all
``ACCEPTED`` is not yet solvable, so its branches are excluded from the pending
set entirely — not merely deprioritized. Readiness is a gate, not a sort weight,
because sorting never-ready branches to the back would still pop them and burn
budget on obligations the search cannot yet close. When a helper accepts, the
parent re-enters pending on the next ``update`` (which rebuilds from scratch);
when a helper goes BLOCKED the parent never re-enters and ``has_work`` returns
False, terminating the loop. The single-root baseline has no dependencies, so
every root branch is always ready and behaviour is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...proof_system.workspace import BranchStatus, ObligationStatus

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


#: Obligation statuses that still need proof work. ACCEPTED / BLOCKED /
#: SUPERSEDED are terminal for scheduling: an accepted obligation has nothing
#: left to prove, a blocked one cannot be pursued, and a superseded one has been
#: replaced by a newer version whose own branch carries the work.
_SOLVABLE_STATUSES: frozenset[ObligationStatus] = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)


def _is_ready(branch: ProofBranch, workspace: ProofWorkspace) -> bool:
    """True iff ``branch``'s obligation can be productively worked now.

    Three conditions, all read-only over the workspace:

    * the branch is ACTIVE (DORMANT / SUPERSEDED / BLOCKED / ACCEPTED retire it);
    * the obligation it pins is still solvable (OPEN / IN_PROGRESS);
    * every dependency of that obligation resolves (via ``by_id``) to an
      ``ACCEPTED`` obligation — an un-proven helper means the parent is not yet
      attackable.

    A missing obligation (a branch pinning a stale id) is not ready. The single
    root has no dependencies, so it is always ready when ACTIVE.
    """
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
        """Load all *ready* branches of the workspace as pending nodes.

        Ready = ACTIVE and whose obligation is solvable with all dependencies
        ACCEPTED (see :func:`_is_ready`). Branches whose helpers are not yet
        accepted are excluded; they re-enter on a later ``update`` once their
        dependencies close.
        """
        self._pending = []
        self._pending_ids = set()
        self._popped_this_round = set()
        for branch in workspace.branches:
            if _is_ready(branch, workspace):
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
        *,
        attempted_branch_ids: tuple[str, ...] = (),
    ) -> None:
        """Refresh the pending set after a reducer transition.

        Rebuilds ``_pending`` from the workspace's current *ready* branches
        (ACTIVE and obligation-solvable with all dependencies ACCEPTED),
        excluding any already popped this round. Retired branches drop naturally
        (no longer ACTIVE); not-yet-ready branches (a helper still open) are
        excluded and re-enter once their dependencies close. Resets the per-round
        popped set so the just-tried branch can be retried next round if it
        stayed ready.
        """
        del branch_id, accepted  # status in the workspace is authoritative
        self._popped_this_round.update(attempted_branch_ids)

        ready = [
            branch
            for branch in workspace.branches
            if _is_ready(branch, workspace)
        ]
        # Preserve round-level fairness: branches already tried in this round
        # stay out while another ready branch remains. Once every ready branch
        # has had a turn, begin a fresh round.
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
