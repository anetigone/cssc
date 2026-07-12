"""Small runtime budget helpers for controller loops."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..proof_system.base import BudgetSlice


class BudgetExhausted(RuntimeError):
    """Raised when a caller asks for budget after it has been exhausted."""


@dataclass(frozen=True)
class BudgetConfig:
    """Simple limits for a proof-search run."""

    max_checks: int = 8
    max_model_calls: int = 4
    per_check_timeout_seconds: float = 10.0
    max_elapsed_seconds: float | None = None
    # Phase 9 action-runtime limits.  They deliberately live beside the
    # existing coarse limits so minimal mode does not need to import any
    # structured-runtime types.  Only the opt-in action frontier consumes
    # them through UnifiedBudgetSnapshot.
    max_input_tokens: float | None = None
    max_output_tokens: float | None = None
    max_billed_tokens: float | None = None
    max_api_cost_usd: float | None = None
    global_reserve_checks: int = 0
    global_reserve_model_requests: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_checks", "max_model_calls", "per_check_timeout_seconds",
            "max_elapsed_seconds", "max_input_tokens", "max_output_tokens",
            "max_billed_tokens", "max_api_cost_usd", "global_reserve_checks",
            "global_reserve_model_requests",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class BudgetSnapshot:
    checks_used: int
    model_calls_used: int
    elapsed_seconds: float
    remaining_checks: int
    remaining_model_calls: int
    exhausted_reason: str | None = None


class BudgetManager:
    """Tracks coarse spending without owning controller policy."""

    def __init__(self, config: BudgetConfig | None = None) -> None:
        self.config = config or BudgetConfig()
        self._started = time.perf_counter()
        self._checks_used = 0
        self._model_calls_used = 0

    @property
    def checks_used(self) -> int:
        return self._checks_used

    @property
    def model_calls_used(self) -> int:
        return self._model_calls_used

    def can_call_model(self) -> bool:
        return self.exhausted_reason(ignore_model=False, ignore_checks=True) is None

    def can_check(self) -> bool:
        return self.exhausted_reason(ignore_model=True, ignore_checks=False) is None

    def reserve_model_call(self) -> None:
        reason = self.exhausted_reason(ignore_checks=True)
        if reason is not None:
            raise BudgetExhausted(reason)
        self._model_calls_used += 1

    def reserve_check(self) -> BudgetSlice:
        reason = self.exhausted_reason(ignore_model=True)
        if reason is not None:
            raise BudgetExhausted(reason)
        self._checks_used += 1
        return BudgetSlice(timeout_seconds=self.config.per_check_timeout_seconds)

    def exhausted_reason(
        self,
        *,
        ignore_model: bool = False,
        ignore_checks: bool = False,
    ) -> str | None:
        if self.config.max_elapsed_seconds is not None:
            if self.elapsed_seconds() >= self.config.max_elapsed_seconds:
                return "elapsed_time"
        if not ignore_checks and self._checks_used >= self.config.max_checks:
            return "checks"
        if not ignore_model and self._model_calls_used >= self.config.max_model_calls:
            return "model_calls"
        return None

    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self._started

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            checks_used=self._checks_used,
            model_calls_used=self._model_calls_used,
            elapsed_seconds=self.elapsed_seconds(),
            remaining_checks=max(0, self.config.max_checks - self._checks_used),
            remaining_model_calls=max(0, self.config.max_model_calls - self._model_calls_used),
            exhausted_reason=self.exhausted_reason(),
        )
