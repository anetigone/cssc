"""Frozen configuration for the opt-in action-level runtime."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionCostSource(str, Enum):
    AUTO = "auto"
    STATIC = "static"
    EMPIRICAL = "empirical"


@dataclass(frozen=True)
class ActionRuntimeConfig:
    cost_source: ActionCostSource = ActionCostSource.AUTO
    remaining_budget_policy: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "cost_source": self.cost_source.value,
            "remaining_budget_policy": self.remaining_budget_policy,
        }
