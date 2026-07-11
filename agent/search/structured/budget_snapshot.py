"""Unified, read-only remaining-budget snapshots for Phase 9.3.

The snapshot is built once at an action-selection boundary and handed to both
proposal generation and action execution.  It does not reserve anything; the
existing :class:`BudgetManager` remains the only mutating budget authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from agent.search.budget import BudgetSnapshot
from agent.search.cost_ledger import CostLedger, MeasurementStatus

from .action_frontier import CostEstimate, Estimate
from .budget_hints import ObligationBudgetHint


class BudgetValueStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    UNBOUNDED = "unbounded"


@dataclass(frozen=True)
class BudgetDimension:
    """A resource limit and its current measured consumption.

    A missing limit is ``UNBOUNDED``; unavailable consumption is ``UNKNOWN``.
    These facts remain separate so an unbounded-but-unmeasured dimension cannot
    be presented as a misleading 100% remaining ratio.
    """

    limit: float | None
    spent: float | None
    limit_status: BudgetValueStatus
    spent_status: BudgetValueStatus
    remaining: float | None
    remaining_ratio: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "limit": self.limit, "spent": self.spent,
            "limit_status": self.limit_status.value,
            "spent_status": self.spent_status.value,
            "remaining": self.remaining, "remaining_ratio": self.remaining_ratio,
        }


@dataclass(frozen=True)
class ActionBudgetLimits:
    """Configured hard limits beyond the legacy checks/model-call limits."""

    max_input_tokens: float | None = None
    max_output_tokens: float | None = None
    max_billed_tokens: float | None = None
    max_elapsed_seconds: float | None = None
    max_api_cost_usd: float | None = None
    global_reserve_checks: int = 0
    global_reserve_model_requests: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_input_tokens", "max_output_tokens", "max_billed_tokens",
            "max_elapsed_seconds", "max_api_cost_usd",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class ObligationBudgetContext:
    obligation_id: str
    soft_checks: int
    soft_model_requests: int
    borrowed_checks: int
    borrowed_model_requests: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "obligation_id": self.obligation_id,
            "soft_checks": self.soft_checks,
            "soft_model_requests": self.soft_model_requests,
            "borrowed_checks": self.borrowed_checks,
            "borrowed_model_requests": self.borrowed_model_requests,
        }


@dataclass(frozen=True)
class UnifiedBudgetSnapshot:
    """One consistent view used by every action policy in a selection round."""

    model_requests: BudgetDimension
    input_tokens: BudgetDimension
    output_tokens: BudgetDimension
    billed_tokens: BudgetDimension
    checks: BudgetDimension
    wall_time: BudgetDimension
    api_cost_usd: BudgetDimension
    obligations: Mapping[str, ObligationBudgetContext] = field(default_factory=dict)
    global_reserve_checks: int = 0
    global_reserve_model_requests: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "model_requests": self.model_requests.to_dict(),
            "input_tokens": self.input_tokens.to_dict(),
            "output_tokens": self.output_tokens.to_dict(),
            "billed_tokens": self.billed_tokens.to_dict(),
            "checks": self.checks.to_dict(),
            "wall_time": self.wall_time.to_dict(),
            "api_cost_usd": self.api_cost_usd.to_dict(),
            "obligations": {key: value.to_dict() for key, value in self.obligations.items()},
            "global_reserve_checks": self.global_reserve_checks,
            "global_reserve_model_requests": self.global_reserve_model_requests,
        }


@dataclass(frozen=True)
class BudgetAdmission:
    allowed: bool
    rejected_dimensions: tuple[str, ...] = ()
    not_compared_dimensions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "rejected_dimensions": list(self.rejected_dimensions),
            "not_compared_dimensions": list(self.not_compared_dimensions),
        }


def build_unified_budget_snapshot(
    budget: BudgetSnapshot,
    ledger: CostLedger | None,
    *,
    limits: ActionBudgetLimits = ActionBudgetLimits(),
    obligation_hints: tuple[ObligationBudgetHint, ...] = (),
) -> UnifiedBudgetSnapshot:
    """Merge legacy counters with ledger measurements without inventing zeros."""
    totals = ledger.reconcile().totals if ledger is not None else {}
    model_total = budget.model_calls_used + budget.remaining_model_calls
    check_total = budget.checks_used + budget.remaining_checks
    return UnifiedBudgetSnapshot(
        model_requests=_known_dimension(model_total, budget.model_calls_used),
        checks=_known_dimension(check_total, budget.checks_used),
        wall_time=_dimension(limits.max_elapsed_seconds, budget.elapsed_seconds, BudgetValueStatus.KNOWN),
        input_tokens=_ledger_dimension(limits.max_input_tokens, totals.get("input_tokens")),
        output_tokens=_ledger_dimension(limits.max_output_tokens, totals.get("output_tokens")),
        billed_tokens=_ledger_dimension(limits.max_billed_tokens, totals.get("billed_tokens")),
        api_cost_usd=_ledger_dimension(limits.max_api_cost_usd, totals.get("api_cost_usd")),
        obligations={
            hint.obligation_id: ObligationBudgetContext(
                obligation_id=hint.obligation_id,
                soft_checks=hint.soft_checks,
                soft_model_requests=hint.soft_model_calls,
                borrowed_checks=hint.borrowed_checks,
                borrowed_model_requests=hint.borrowed_model_calls,
            )
            for hint in obligation_hints
        },
        global_reserve_checks=limits.global_reserve_checks,
        global_reserve_model_requests=limits.global_reserve_model_requests,
    )


def admit_estimate(
    snapshot: UnifiedBudgetSnapshot,
    estimate: CostEstimate,
    *,
    reject_unknown: bool = False,
) -> BudgetAdmission:
    """Enforce only known hard constraints; missing dimensions are not compared."""
    pairs = (
        ("model_requests", snapshot.model_requests, estimate.model_requests),
        ("input_tokens", snapshot.input_tokens, estimate.input_tokens),
        ("output_tokens", snapshot.output_tokens, estimate.output_tokens),
        ("billed_tokens", snapshot.billed_tokens, estimate.billed_tokens),
        ("checks", snapshot.checks, estimate.checks),
        # Snapshot wall time is seconds; estimates store checker wall time in ms.
        ("wall_time", snapshot.wall_time, _milliseconds_to_seconds(estimate.checker_wall_ms)),
        ("api_cost_usd", snapshot.api_cost_usd, estimate.api_cost_usd),
    )
    rejected: list[str] = []
    not_compared: list[str] = []
    for name, dimension, action_cost in pairs:
        if dimension.remaining is None or action_cost is None:
            not_compared.append(name)
            if reject_unknown and action_cost is not None and dimension.limit_status is BudgetValueStatus.KNOWN:
                rejected.append(name)
        elif action_cost.value > dimension.remaining:
            rejected.append(name)
    return BudgetAdmission(not rejected, tuple(rejected), tuple(not_compared))


def _milliseconds_to_seconds(value: Estimate | None) -> Estimate | None:
    if value is None:
        return None
    return Estimate(value.value / 1000.0)


def _known_dimension(limit: float, spent: float) -> BudgetDimension:
    remaining = max(0.0, float(limit) - float(spent))
    ratio = remaining / float(limit) if limit else 0.0
    return BudgetDimension(
        limit=float(limit), spent=float(spent), limit_status=BudgetValueStatus.KNOWN,
        spent_status=BudgetValueStatus.KNOWN, remaining=remaining, remaining_ratio=ratio,
    )


def _dimension(limit: float | None, spent: float, spent_status: BudgetValueStatus) -> BudgetDimension:
    if limit is None:
        return BudgetDimension(None, spent, BudgetValueStatus.UNBOUNDED, spent_status, None, None)
    remaining = max(0.0, limit - spent)
    return BudgetDimension(limit, spent, BudgetValueStatus.KNOWN, spent_status, remaining, remaining / limit if limit else 0.0)


def _ledger_dimension(limit: float | None, total: object | None) -> BudgetDimension:
    if total is None:
        spent = None
        spent_status = BudgetValueStatus.UNKNOWN
    else:
        measurement = total.measurement  # LedgerTotal; kept duck-typed to avoid trace coupling.
        if measurement.status in {MeasurementStatus.OBSERVED, MeasurementStatus.ESTIMATED}:
            spent = float(measurement.value)
            spent_status = BudgetValueStatus.KNOWN
        else:
            spent = None
            spent_status = BudgetValueStatus.UNKNOWN
    if limit is None:
        return BudgetDimension(None, spent, BudgetValueStatus.UNBOUNDED, spent_status, None, None)
    if spent is None:
        return BudgetDimension(limit, None, BudgetValueStatus.KNOWN, spent_status, None, None)
    remaining = max(0.0, limit - spent)
    return BudgetDimension(limit, spent, BudgetValueStatus.KNOWN, spent_status, remaining, remaining / limit if limit else 0.0)
