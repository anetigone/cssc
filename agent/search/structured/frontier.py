"""Mutable frontier scheduler for structured search."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agent.proof_system.workspace import BranchStatus, ObligationStatus, SearchActionKind

from .costing import _obligation_closure

if TYPE_CHECKING:
    from agent.proof_system.workspace import ObligationGraph, ProofBranch, ProofWorkspace


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
    described in ``tmp/phase8_plan.md`` §3. ``COST_AWARE_V2`` (Phase 8.3) layers
    a soft-budget overdraft signal on top of V1's cost ordering: a branch that
    has spent past its per-obligation soft envelope is deprioritised, while the
    inherited V1 dimensions (cheap action, non-stalled, high unlock value)
    break ties among non-overdraft peers. Minimal mode never imports this enum.
    """

    LEGACY = "legacy"
    COST_AWARE_V1 = "cost_aware_v1"
    COST_AWARE_V2 = "cost_aware_v2"


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
    #: consulted by the cost-aware keys; legacy key ignores it so legacy nodes
    #: order identically whether or not it is populated.
    next_action_cost: int = 0
    #: Soft-budget envelope for this branch's obligation (Phase 8.3). Only the
    #: ``cost_aware_v2`` key reads them; legacy/v1 keys ignore the fields, so
    #: they default to zero and never perturb earlier orderings.
    soft_checks: int = 0
    soft_model_calls: int = 0
    #: True unlock value (active dependents count) for this obligation (Phase
    #: 8.3). V1 used ``depth_from_root`` as a proxy; V2 uses the real count so
    #: the ordering is driven by the same hint projection as ``budget_hints``.
    unlock_value_rank: int = 0
    #: Branch-local realised checks. Unlike ``attempt_count``, this excludes
    #: evidence inherited from parent branches, so fresh repair / representation
    #: forks are not treated as having already spent their parent's checks.
    local_attempt_count: int = 0


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


@dataclass(frozen=True)
class BudgetHintDefaults:
    """Tuning knobs for the soft-budget envelope (Phase 8.3 §4).

    All integer so the resulting envelopes are directly comparable in a
    deterministic frontier key without any float arithmetic. Consumed by
    :func:`soft_envelope_for_obligation` (and, via :mod:`budget_hints`, by the
    result-level ``metadata["budget_hints"]`` projection) so V2 ordering and
    the recorded hint share one source of truth.
    """

    base_soft_checks: int = 1
    base_soft_model_calls: int = 1
    # root / unlock value bonus (plan §4.1)
    root_bonus_checks: int = 1
    root_bonus_model_calls: int = 1
    per_unlock_bonus_checks: int = 1
    per_unlock_bonus_model_calls: int = 1
    # capability audit is a cheap probe: 0 model calls (plan §4.2)
    capability_soft_checks: int = 1
    capability_soft_model_calls: int = 0
    # a stalled branch is deprioritised via the key; its envelope collapses (§4.3)
    stalled_soft_checks: int = 0
    stalled_soft_model_calls: int = 0
    # an obligation adjacent to an accepted helper keeps recovery budget (§4.4)
    accepted_neighbor_bonus_checks: int = 1
    accepted_neighbor_bonus_model_calls: int = 1
    stall_threshold: int = STALL_THRESHOLD


def _dependents_count(graph: ObligationGraph) -> dict[str, int]:
    """Map obligation_id -> number of active obligations depending on it.

    Edges run parent -> helper (``dependency_ids``), so a helper depended on by
    many parents has a higher unlock value. The root obligation is not listed
    as a dependency of anyone, so it earns its own bonus downstream.
    """
    counts: dict[str, int] = defaultdict(int)
    for parent in graph.active():
        for dependency_id in parent.dependency_ids:
            counts[dependency_id] += 1
    return dict(counts)


def _next_action_is_capability(branch: ProofBranch | None) -> bool:
    """True if the branch's next action is expected to be a capability probe.

    Mirrors the static-cost read in :func:`_next_action_cost`: a pending
    capability test ranks the next action as ``RUN_CAPABILITY_TEST``.
    """
    if branch is None:
        return False
    return _next_action_cost(branch) == _ACTION_INCREMENTAL_COST[
        SearchActionKind.RUN_CAPABILITY_TEST
    ]


def _has_accepted_neighbor(
    obligation_id: str, workspace: ProofWorkspace
) -> bool:
    """True if any accepted fact lives in ``obligation_id``'s dependency closure.

    Closure edges run parent -> helper (reused from :mod:`costing`), so this
    detects progress among the helpers this obligation ultimately depends on,
    plus itself: a partial result nearby should keep recovery budget (§4.4).
    """
    if not workspace.accepted_facts:
        return False
    closure = _obligation_closure(obligation_id, workspace.obligation_graph)
    return any(
        fact.obligation_id in closure for fact in workspace.accepted_facts
    )


def _branches_for_obligation(
    obligation_id: str, workspace: ProofWorkspace
) -> tuple[ProofBranch, ...]:
    """Current-version branches working ``obligation_id``.

    Hint derivation must not assume every obligation has a branch (a freshly
    decomposed helper may be unworked). Prefer ACTIVE branches pinned to the
    obligation's current version, because older SUPERSEDED / DORMANT branches
    remain in the workspace as provenance and must not shape current hints.
    """
    obligation = workspace.obligation_graph.by_id(obligation_id)
    if obligation is None:
        return ()
    current = tuple(
        branch
        for branch in workspace.branches
        if branch.obligation_id == obligation_id
        and branch.obligation_version == obligation.version
    )
    active = tuple(branch for branch in current if branch.status == BranchStatus.ACTIVE)
    return active or current


def soft_envelope_for_obligation(
    obligation_id: str,
    workspace: ProofWorkspace,
    config: BudgetHintDefaults = BudgetHintDefaults(),
    *,
    branch: ProofBranch | None = None,
) -> tuple[int, int]:
    """Return ``(soft_checks, soft_model_calls)`` for one obligation.

    Pure projection over the public workspace surface. Capability / stalled
    states *replace the base envelope* (an audit needs no model call; a stuck
    branch is starved toward zero), while root / unlock / accepted-neighbour
    states *add* to whatever base was chosen, so a stalled root is still ranked
    ahead of its helpers rather than buried. Shared by the V2 ``FrontierNode``
    derivation and the result-level :mod:`budget_hints` projection.
    """
    graph = workspace.obligation_graph
    obligation = graph.by_id(obligation_id)
    if obligation is None:
        return (config.base_soft_checks, config.base_soft_model_calls)

    is_root = obligation_id in workspace.root_obligation_ids
    unlock_value = _dependents_count(graph).get(obligation_id, 0)
    branches = (branch,) if branch is not None else _branches_for_obligation(
        obligation_id, workspace
    )
    is_capability = any(_next_action_is_capability(item) for item in branches)
    is_stalled = bool(branches) and all(
        _stalled_streak(item) >= config.stall_threshold for item in branches
    )
    has_accepted_neighbor = _has_accepted_neighbor(obligation_id, workspace)

    # Capability / stalled *replace the base envelope* (an audit needs no model
    # call; a stuck branch is starved toward zero), but the additive structural
    # bonuses (root priority, unlock value, accepted-neighbour recovery) still
    # apply on top, so a stalled root is not buried beneath every helper.
    if is_capability:
        base_checks = config.capability_soft_checks
        base_model_calls = config.capability_soft_model_calls
    elif is_stalled:
        base_checks = config.stalled_soft_checks
        base_model_calls = config.stalled_soft_model_calls
    else:
        base_checks = config.base_soft_checks
        base_model_calls = config.base_soft_model_calls

    soft_checks = base_checks
    soft_model_calls = base_model_calls
    if is_root:
        soft_checks += config.root_bonus_checks
        soft_model_calls += config.root_bonus_model_calls
    soft_checks += config.per_unlock_bonus_checks * unlock_value
    soft_model_calls += config.per_unlock_bonus_model_calls * unlock_value
    if has_accepted_neighbor:
        soft_checks += config.accepted_neighbor_bonus_checks
        soft_model_calls += config.accepted_neighbor_bonus_model_calls
    return (soft_checks, soft_model_calls)


def _node_for(branch: ProofBranch, workspace: ProofWorkspace) -> FrontierNode:
    soft_checks, soft_model_calls = soft_envelope_for_obligation(
        branch.obligation_id, workspace, branch=branch
    )
    unlock_value_rank = _dependents_count(workspace.obligation_graph).get(
        branch.obligation_id, 0
    )
    if branch.obligation_id in workspace.root_obligation_ids:
        # Roots have no inbound dependency edges; floor them so V2 ranks a root
        # above any helper regardless of the helper's dependent count.
        unlock_value_rank = max(unlock_value_rank, len(workspace.obligation_graph.active()))
    return FrontierNode(
        branch_id=branch.branch_id,
        obligation_id=branch.obligation_id,
        depth_from_root=_depth_from_root(branch, workspace),
        attempt_count=_attempt_count(branch),
        last_goal_fingerprints=_branch_goal_fingerprints(branch),
        stalled_streak=_stalled_streak(branch),
        next_action_cost=_next_action_cost(branch),
        soft_checks=soft_checks,
        soft_model_calls=soft_model_calls,
        unlock_value_rank=unlock_value_rank,
        local_attempt_count=_branch_local_attempt_count(branch, workspace),
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
    return len(_observation_refs(branch))


def _observation_refs(branch: ProofBranch) -> set[str]:
    """Distinct evidence refs recorded on ``branch``."""
    return {
        obs.raw_evidence_ref for obs in branch.observations if obs.raw_evidence_ref
    }


def _branch_local_attempt_count(branch: ProofBranch, workspace: ProofWorkspace) -> int:
    """Number of evidence refs introduced by this branch, excluding ancestors."""
    ancestor_refs: set[str] = set()
    by_id = {item.branch_id: item for item in workspace.branches}
    current_id = branch.parent_branch_id
    seen: set[str] = set()
    while current_id is not None and current_id in by_id and current_id not in seen:
        seen.add(current_id)
        parent = by_id[current_id]
        ancestor_refs.update(_observation_refs(parent))
        current_id = parent.parent_branch_id
    return len(_observation_refs(branch) - ancestor_refs)


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
        if self._policy is FrontierPolicy.COST_AWARE_V2:
            key = _soft_budget_priority_key
        elif self._policy is FrontierPolicy.COST_AWARE_V1:
            key = _cost_aware_priority_key
        else:
            key = _priority_key
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


def _soft_budget_priority_key(
    node: FrontierNode,
) -> tuple[int, int, int, int, int, int, str]:
    """Deterministic soft-budget sort key (Phase 8.3 §4).

    ``cost_aware_v1`` ordering with a leading overdraft signal: a branch that
    has spent past its per-obligation soft envelope (``attempt_count`` against
    ``soft_checks``) is deprioritised before any V1 dimension is consulted, so
    no single obligation starves the loop. All ascending so smaller pops first:

    * ``overdraft_checks`` --- ``max(0, attempt_count - soft_checks)``; zero for
      branches still inside their envelope;
    * then the full V1 tuple (cheap next action, non-stalled, high unlock value,
      fewer attempts, shallower, branch_id) to break ties among non-overdraft
      peers. ``unlock_value_rank`` here is the real dependents count on the node
      (V1 used ``depth_from_root`` as a proxy), negated because a *higher* unlock
      value should pop *earlier* in an otherwise ascending tuple.
    """
    overdraft_checks = max(0, node.local_attempt_count - node.soft_checks)
    stalled_penalty = 1 if node.stalled_streak >= STALL_THRESHOLD else 0
    return (
        overdraft_checks,
        node.next_action_cost,
        stalled_penalty,
        -node.unlock_value_rank,
        node.local_attempt_count,
        node.depth_from_root,
        node.branch_id,
    )
