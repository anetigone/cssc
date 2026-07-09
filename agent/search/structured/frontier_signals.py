"""Pure workspace projections used by structured frontier scheduling."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from agent.proof_system.workspace import BranchStatus, ObligationStatus, SearchActionKind

from .costing import _obligation_closure
from .frontier_types import (
    ACTION_INCREMENTAL_COST,
    BudgetHintDefaults,
    FrontierNode,
)

if TYPE_CHECKING:
    from agent.proof_system.workspace import ObligationGraph, ProofBranch, ProofWorkspace


def branch_goal_fingerprints(branch: ProofBranch) -> tuple[str, ...]:
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
    return tuple(sorted(obs.goal_fingerprint for obs in latest if obs.goal_fingerprint))


def depth_from_root(branch: ProofBranch, workspace: ProofWorkspace) -> int:
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


def next_action_cost(branch: ProofBranch) -> int:
    """Static estimate of this branch's next incremental action cost."""
    for hypothesis in branch.failure_hypotheses:
        for test in hypothesis.proposed_tests:
            if test.target_branch_id != branch.branch_id:
                continue
            cost = ACTION_INCREMENTAL_COST.get(test.kind)
            if cost is not None:
                return cost
    return ACTION_INCREMENTAL_COST[SearchActionKind.IMPLEMENT]


def dependents_count(graph: ObligationGraph) -> dict[str, int]:
    """Map obligation_id to the number of active obligations depending on it."""
    counts: dict[str, int] = defaultdict(int)
    for parent in graph.active():
        for dependency_id in parent.dependency_ids:
            counts[dependency_id] += 1
    return dict(counts)


def next_action_is_capability(branch: ProofBranch | None) -> bool:
    """True iff the branch's next action is expected to be a capability probe."""
    if branch is None:
        return False
    return next_action_cost(branch) == ACTION_INCREMENTAL_COST[
        SearchActionKind.RUN_CAPABILITY_TEST
    ]


def progress_likelihood(branch: ProofBranch) -> int:
    """Integer value signal: did the latest attempt change the goal set?"""
    if stalled_streak(branch) != 1:
        return 0
    seen_refs: set[str] = set()
    distinct_refs: list[str] = []
    for obs in reversed(branch.observations):
        if not obs.raw_evidence_ref or obs.raw_evidence_ref in seen_refs:
            continue
        seen_refs.add(obs.raw_evidence_ref)
        distinct_refs.append(obs.raw_evidence_ref)
    return 1 if len(distinct_refs) >= 2 else 0


def information_gain(branch: ProofBranch | None) -> int:
    """Integer value signal: can the next action discriminate hypotheses?"""
    return 1 if next_action_is_capability(branch) else 0


def _has_accepted_neighbor(obligation_id: str, workspace: ProofWorkspace) -> bool:
    if not workspace.accepted_facts:
        return False
    closure = _obligation_closure(obligation_id, workspace.obligation_graph)
    return any(fact.obligation_id in closure for fact in workspace.accepted_facts)


def _branches_for_obligation(
    obligation_id: str, workspace: ProofWorkspace
) -> tuple[ProofBranch, ...]:
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
    """Return ``(soft_checks, soft_model_calls)`` for one obligation."""
    graph = workspace.obligation_graph
    obligation = graph.by_id(obligation_id)
    if obligation is None:
        return (config.base_soft_checks, config.base_soft_model_calls)

    is_root = obligation_id in workspace.root_obligation_ids
    unlock_value = dependents_count(graph).get(obligation_id, 0)
    branches = (branch,) if branch is not None else _branches_for_obligation(
        obligation_id, workspace
    )
    is_capability = any(next_action_is_capability(item) for item in branches)
    is_stalled = bool(branches) and all(
        stalled_streak(item) >= config.stall_threshold for item in branches
    )

    if is_capability:
        soft_checks = config.capability_soft_checks
        soft_model_calls = config.capability_soft_model_calls
    elif is_stalled:
        soft_checks = config.stalled_soft_checks
        soft_model_calls = config.stalled_soft_model_calls
    else:
        soft_checks = config.base_soft_checks
        soft_model_calls = config.base_soft_model_calls

    if is_root:
        soft_checks += config.root_bonus_checks
        soft_model_calls += config.root_bonus_model_calls
    soft_checks += config.per_unlock_bonus_checks * unlock_value
    soft_model_calls += config.per_unlock_bonus_model_calls * unlock_value
    if _has_accepted_neighbor(obligation_id, workspace):
        soft_checks += config.accepted_neighbor_bonus_checks
        soft_model_calls += config.accepted_neighbor_bonus_model_calls
    return (soft_checks, soft_model_calls)


def node_for(branch: ProofBranch, workspace: ProofWorkspace) -> FrontierNode:
    """Build a schedulable node projection for ``branch``."""
    soft_checks, soft_model_calls = soft_envelope_for_obligation(
        branch.obligation_id, workspace, branch=branch
    )
    unlock_value_rank = dependents_count(workspace.obligation_graph).get(
        branch.obligation_id, 0
    )
    if branch.obligation_id in workspace.root_obligation_ids:
        unlock_value_rank = max(
            unlock_value_rank, len(workspace.obligation_graph.active())
        )
    return FrontierNode(
        branch_id=branch.branch_id,
        obligation_id=branch.obligation_id,
        depth_from_root=depth_from_root(branch, workspace),
        attempt_count=attempt_count(branch),
        last_goal_fingerprints=branch_goal_fingerprints(branch),
        stalled_streak=stalled_streak(branch),
        next_action_cost=next_action_cost(branch),
        soft_checks=soft_checks,
        soft_model_calls=soft_model_calls,
        unlock_value_rank=unlock_value_rank,
        local_attempt_count=branch_local_attempt_count(branch, workspace),
        unlock_value=unlock_value_rank,
        progress_likelihood=progress_likelihood(branch),
        information_gain=information_gain(branch),
    )


_SOLVABLE_STATUSES: frozenset[ObligationStatus] = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)


def is_ready(branch: ProofBranch, workspace: ProofWorkspace) -> bool:
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


def attempt_count(branch: ProofBranch) -> int:
    """Number of distinct attempt evidence refs on this branch."""
    return len(observation_refs(branch))


def observation_refs(branch: ProofBranch) -> set[str]:
    """Distinct evidence refs recorded on ``branch``."""
    return {
        obs.raw_evidence_ref for obs in branch.observations if obs.raw_evidence_ref
    }


def branch_local_attempt_count(branch: ProofBranch, workspace: ProofWorkspace) -> int:
    """Number of evidence refs introduced by this branch, excluding ancestors."""
    ancestor_refs: set[str] = set()
    by_id = {item.branch_id: item for item in workspace.branches}
    current_id = branch.parent_branch_id
    seen: set[str] = set()
    while current_id is not None and current_id in by_id and current_id not in seen:
        seen.add(current_id)
        parent = by_id[current_id]
        ancestor_refs.update(observation_refs(parent))
        current_id = parent.parent_branch_id
    return len(observation_refs(branch) - ancestor_refs)


def stalled_streak(branch: ProofBranch) -> int:
    """Number of trailing attempts stuck on the same goal-fingerprint set."""
    observations = branch.observations
    if not observations:
        return 0
    batches: list[tuple[str, ...]] = []
    seen_refs: set[str] = set()
    for obs in reversed(observations):
        if not obs.raw_evidence_ref or obs.raw_evidence_ref in seen_refs:
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
    for _current, previous in zip(batches, batches[1:]):
        if previous == batches[0]:
            streak += 1
        else:
            break
    return streak
