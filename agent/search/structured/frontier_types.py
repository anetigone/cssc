"""Shared data types and constants for structured frontier scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.proof_system.workspace import SearchActionKind


STALL_THRESHOLD = 3
VALUE_SCORE_SCALE = 1024

#: Static expected-incremental-cost weights per action kind (Phase 8.2).
#: Final assembly is run-level cost the controller reserves explicitly, never a
#: branch-local frontier cost.
ACTION_INCREMENTAL_COST: dict[SearchActionKind, int] = {
    SearchActionKind.DECOMPOSE: 0,
    SearchActionKind.PROPOSE_ARGUMENT: 0,
    SearchActionKind.REFINE_ARGUMENT: 0,
    SearchActionKind.CHANGE_REPRESENTATION: 0,
    SearchActionKind.RUN_CAPABILITY_TEST: 1,
    SearchActionKind.IMPLEMENT: 2,
    SearchActionKind.REPAIR_IMPLEMENTATION: 2,
}


class FrontierPolicy(str, Enum):
    """Which priority key the :class:`Frontier` uses to order ready branches."""

    LEGACY = "legacy"
    COST_AWARE_V1 = "cost_aware_v1"
    COST_AWARE_V2 = "cost_aware_v2"
    VALUE_PER_COST_V1 = "value_per_cost_v1"


@dataclass(frozen=True)
class FrontierNode:
    """One schedulable branch/obligation pair."""

    branch_id: str
    obligation_id: str
    depth_from_root: int
    attempt_count: int
    last_goal_fingerprints: tuple[str, ...]
    stalled_streak: int
    next_action_cost: int = 0
    soft_checks: int = 0
    soft_model_calls: int = 0
    unlock_value_rank: int = 0
    local_attempt_count: int = 0
    unlock_value: int = 0
    progress_likelihood: int = 0
    information_gain: int = 0


@dataclass(frozen=True)
class PriorityExplanation:
    """Why the frontier popped one branch when it did."""

    branch_id: str
    policy: str
    expected_incremental_cost: int
    unlock_value: int
    progress_likelihood: int
    information_gain: int
    final_key_or_score: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_id": self.branch_id,
            "policy": self.policy,
            "expected_incremental_cost": self.expected_incremental_cost,
            "unlock_value": self.unlock_value,
            "progress_likelihood": self.progress_likelihood,
            "information_gain": self.information_gain,
            "final_key_or_score": tuple(self.final_key_or_score),
        }


@dataclass(frozen=True)
class BudgetHintDefaults:
    """Tuning knobs for the soft-budget envelope (Phase 8.3 §4)."""

    base_soft_checks: int = 1
    base_soft_model_calls: int = 1
    root_bonus_checks: int = 1
    root_bonus_model_calls: int = 1
    per_unlock_bonus_checks: int = 1
    per_unlock_bonus_model_calls: int = 1
    capability_soft_checks: int = 1
    capability_soft_model_calls: int = 0
    stalled_soft_checks: int = 0
    stalled_soft_model_calls: int = 0
    accepted_neighbor_bonus_checks: int = 1
    accepted_neighbor_bonus_model_calls: int = 1
    stall_threshold: int = STALL_THRESHOLD
