"""Mutable frontier scheduler for structured search."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agent.proof_system.workspace import BranchStatus, ObligationStatus, SearchActionKind

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofBranch, ProofWorkspace


STALL_THRESHOLD = 3

#: Static expected-incremental-cost weights per action kind (Phase 8.2 §3).
#: The value is a single comparable integer (``checks`` and ``model_calls`` are
#: collapsed via a small weight so cheaper-next-action branches rank first).
#: Final assembly is intentionally absent --- it is run-level cost the
#: controller reserves explicitly, never charged to a branch.
_ACTION_INCREMENTAL_COST: dict[SearchActionKind, int] = {
    SearchActionKind.DECOMPOSE: 0,
    SearchActionKind.PROPOSE_ARGUMENT: 0,
    SearchActionKind.REFINE_ARGUMENT: 0,
    SearchActionKind.CHANGE_REPRESENTATION: 0,
    SearchActionKind.RUN_CAPABILITY_TEST: 1,
    SearchActionKind.IMPLEMENT: 2,
    SearchActionKind.REPAIR_IMPLEMENTATION: 2,
}


class FrontierPolicy(str, Enum):
    """Which priority key the :class:`Frontier` uses to order ready branches.

    ``LEGACY`` is the default and must stay byte-for-byte stable so existing
    structured traces replay identically. ``COST_AWARE_V1`` reorders the ready
    set (readiness is still a gate, never a weight) using a deterministic tuple
    described in ``tmp/phase8_plan.md`` §3. Minimal mode never imports this
    enum.
    """

    LEGACY = "legacy"
    COST_AWARE_V1 = "cost_aware_v1"


@dataclass(frozen=True)
class FrontierNode:
    """One schedulable branch/obligation pair."""

    branch_id: str
    obligation_id: str
    depth_from_root: int
    attempt_count: int
    last_goal_fingerprints: tuple[str, ...]
    stalled_streak: int
    #: Static estimate of the next action's incremental cost (Phase 8.2). Only
    #: consulted by the cost-aware key; legacy key ignores it so legacy nodes
    #: order identically whether or not it is populated.
    next_action_cost: int = 0


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


def _next_action_cost(branch: ProofBranch) -> int:
    """Static estimate of this branch's next incremental action cost.

    Mirrors the controller's own ``_select_test_action`` ordering: a branch
    with a pending capability test proposal is expected to run that cheap
    probe next (1 check, 0 model calls); otherwise it will implement or repair
    (1 check + 1 model call). Structural-only branches cost nothing to
    advance. The value is a comparable integer, not a token count.
    """
    for hypothesis in branch.failure_hypotheses:
        for test in hypothesis.proposed_tests:
            if test.target_branch_id != branch.branch_id:
                continue
            cost = _ACTION_INCREMENTAL_COST.get(test.kind)
            if cost is not None:
                return cost
    return _ACTION_INCREMENTAL_COST[SearchActionKind.IMPLEMENT]


def _node_for(branch: ProofBranch, workspace: ProofWorkspace) -> FrontierNode:
    return FrontierNode(
        branch_id=branch.branch_id,
        obligation_id=branch.obligation_id,
        depth_from_root=_depth_from_root(branch, workspace),
        attempt_count=_attempt_count(branch),
        last_goal_fingerprints=_branch_goal_fingerprints(branch),
        stalled_streak=_stalled_streak(branch),
        next_action_cost=_next_action_cost(branch),
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

    def __init__(
        self, *, policy: FrontierPolicy = FrontierPolicy.LEGACY
    ) -> None:
        self._policy = policy
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

    @property
    def policy(self) -> FrontierPolicy:
        """The priority policy this frontier orders ready branches by."""
        return self._policy

    def pop(self) -> FrontierNode:
        """Return and remove the highest-priority pending node."""
        if not self._pending:
            raise StopIteration("frontier is empty")
        key = (
            _cost_aware_priority_key
            if self._policy is FrontierPolicy.COST_AWARE_V1
            else _priority_key
        )
        self._pending.sort(key=key)
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


def _cost_aware_priority_key(
    node: FrontierNode,
) -> tuple[int, int, int, int, int, str]:
    """Deterministic cost-aware sort key (Phase 8.2 §3).

    Tuple order, all ascending so smaller pops first:

    * ``expected_incremental_cost`` --- static cost of the branch's next action;
    * ``stalled_penalty`` --- 1 once the branch exceeds :data:`STALL_THRESHOLD`,
      so stuck branches lose to peers that can still make cheap progress;
    * ``unlock_value_rank`` --- depth-from-root: shallower branches unlock more
      parents, so they rank first (smaller depth pops earlier);
    * ``attempt_count`` --- prefer fewer-spent branches at equal cost/value;
    * ``depth_from_root`` --- secondary structural tie-break;
    * ``branch_id`` --- final tie-breaker for trace reproducibility.
    """
    stalled_penalty = 1 if node.stalled_streak >= STALL_THRESHOLD else 0
    unlock_value_rank = node.depth_from_root
    return (
        node.next_action_cost,
        stalled_penalty,
        unlock_value_rank,
        node.attempt_count,
        node.depth_from_root,
        node.branch_id,
    )
