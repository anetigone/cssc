"""Priority keys for structured frontier scheduling."""

from __future__ import annotations

from collections.abc import Callable

from .frontier_types import (
    STALL_THRESHOLD,
    VALUE_SCORE_SCALE,
    FrontierNode,
    FrontierPolicy,
)

PriorityKey = Callable[[FrontierNode], tuple]


def select_priority_key(policy: FrontierPolicy) -> PriorityKey:
    if policy is FrontierPolicy.VALUE_PER_COST_V1:
        return value_per_cost_priority_key
    if policy is FrontierPolicy.COST_AWARE_V2:
        return soft_budget_priority_key
    if policy is FrontierPolicy.COST_AWARE_V1:
        return cost_aware_priority_key
    return legacy_priority_key


def legacy_priority_key(node: FrontierNode) -> tuple[int, int, int, str]:
    """Stable legacy sort key."""
    return (
        node.stalled_streak,
        node.depth_from_root,
        node.attempt_count,
        node.branch_id,
    )


def cost_aware_priority_key(
    node: FrontierNode,
) -> tuple[int, int, int, int, int, str]:
    """Deterministic cost-aware sort key."""
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


def soft_budget_priority_key(
    node: FrontierNode,
) -> tuple[int, int, int, int, int, int, str]:
    """Deterministic soft-budget sort key."""
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


def value_per_cost_priority_key(
    node: FrontierNode,
) -> tuple[int, int, int, int, int, int, str]:
    """Deterministic fixed-point value/cost sort key."""
    stalled_penalty = 1 if node.stalled_streak >= STALL_THRESHOLD else 0
    overdraft_checks = max(0, node.local_attempt_count - node.soft_checks)
    expected_cost = max(1, node.next_action_cost)
    value_numerator = (
        node.unlock_value * node.progress_likelihood * node.information_gain
    )
    value_score = (value_numerator * VALUE_SCORE_SCALE) // expected_cost
    return (
        stalled_penalty,
        overdraft_checks,
        -value_score,
        node.next_action_cost,
        node.local_attempt_count,
        node.depth_from_root,
        node.branch_id,
    )
