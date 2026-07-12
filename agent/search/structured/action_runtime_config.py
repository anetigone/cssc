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

    def __post_init__(self) -> None:
        if not isinstance(self.cost_source, ActionCostSource):
            try:
                object.__setattr__(self, "cost_source", ActionCostSource(self.cost_source))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unknown action cost source: {self.cost_source!r}") from exc
        if not isinstance(self.remaining_budget_policy, bool):
            raise TypeError("remaining_budget_policy must be bool")

    def to_dict(self) -> dict[str, object]:
        return {
            "cost_source": self.cost_source.value,
            "remaining_budget_policy": self.remaining_budget_policy,
        }
