"""Opt-in cheap-to-strong model routing for structured proof actions.

Routing is an execution policy, not a second proof agent: both tiers emit the
same ``StructuredActionProposal`` protocol and share the same ledger/budget.
The router is pure so every escalation decision can be replayed from the
visible action context and frozen budget snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from agent.proof_system.workspace import SearchActionKind
from agent.search.action import ActionGenerationRequest

from .action_frontier import CostEstimate, Estimate
from .budget_snapshot import BudgetAdmission, UnifiedBudgetSnapshot, admit_estimate


class ModelTier(str, Enum):
    CHEAP = "cheap"
    STRONG = "strong"


@dataclass(frozen=True)
class ModelRouterConfig:
    """Frozen routing rules. Disabled routing is a strict single-tier policy."""

    enabled: bool = False
    cheap_model: str | None = None
    strong_model: str | None = None
    cheap_failures_for_escalation: int = 2
    validation_failures_for_escalation: int = 2
    stalled_streak_for_escalation: int = 3
    min_unlock_value_for_escalation: int = 1
    max_cheap_success_rate_for_escalation: float = 0.25
    strong_action_kinds: frozenset[SearchActionKind] = frozenset({
        SearchActionKind.DECOMPOSE,
        SearchActionKind.PROPOSE_ARGUMENT,
        SearchActionKind.REFINE_ARGUMENT,
        SearchActionKind.CHANGE_REPRESENTATION,
    })
    cheap_cost: CostEstimate = field(default_factory=lambda: CostEstimate(
        model_requests=Estimate(1), source="prior",
        estimator_version="phase9.4-cheap-prior-v1",
    ))
    strong_cost: CostEstimate = field(default_factory=lambda: CostEstimate(
        model_requests=Estimate(1), checks=Estimate(1), source="prior",
        estimator_version="phase9.4-strong-prior-v1",
    ))

    def __post_init__(self) -> None:
        if self.cheap_failures_for_escalation < 1:
            raise ValueError("cheap_failures_for_escalation must be positive")
        if self.validation_failures_for_escalation < 1:
            raise ValueError("validation_failures_for_escalation must be positive")
        if self.stalled_streak_for_escalation < 1:
            raise ValueError("stalled_streak_for_escalation must be positive")
        if not 0 <= self.max_cheap_success_rate_for_escalation <= 1:
            raise ValueError("max_cheap_success_rate_for_escalation must be in [0, 1]")


@dataclass(frozen=True)
class RoutingContext:
    """Only facts visible before the action's future checker result."""

    action_kind: SearchActionKind
    goal_fingerprint: str | None = None
    cheap_failures_on_fingerprint: int = 0
    proposal_validation_failures: int = 0
    stalled_streak: int = 0
    unlock_value: int = 0
    cheap_success_rate: float | None = None
    has_trusted_cheap_cached_action: bool = False
    is_low_cost_capability_probe: bool = False


@dataclass(frozen=True)
class RouteDecision:
    tier: ModelTier
    model: str | None
    reason: str
    escalation_requested: bool
    escalation_granted: bool
    budget_admission: BudgetAdmission | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier.value, "model": self.model, "reason": self.reason,
            "escalation_requested": self.escalation_requested,
            "escalation_granted": self.escalation_granted,
            "budget_admission": self.budget_admission.to_dict() if self.budget_admission else None,
        }


def route_model(
    context: RoutingContext,
    budget: UnifiedBudgetSnapshot,
    *,
    config: ModelRouterConfig = ModelRouterConfig(),
) -> RouteDecision:
    """Choose a tier, rejecting escalation when its conservative cost cannot fit."""
    if not config.enabled:
        return RouteDecision(ModelTier.CHEAP, config.cheap_model, "routing_disabled", False, False)
    if context.has_trusted_cheap_cached_action:
        return RouteDecision(ModelTier.CHEAP, config.cheap_model, "trusted_cheap_cache", False, False)
    if context.is_low_cost_capability_probe:
        return RouteDecision(ModelTier.CHEAP, config.cheap_model, "low_cost_capability_probe", False, False)

    reason = _escalation_reason(context, config)
    if reason is None:
        return RouteDecision(ModelTier.CHEAP, config.cheap_model, "cheap_default", False, False)
    admission = admit_estimate(
        budget, config.strong_cost, reject_unknown=True
    )
    if not admission.allowed:
        return RouteDecision(
            ModelTier.CHEAP, config.cheap_model, f"strong_budget_rejected:{reason}",
            True, False, admission,
        )
    return RouteDecision(ModelTier.STRONG, config.strong_model, reason, True, True, admission)


def routing_metadata(decision: RouteDecision) -> dict[str, object]:
    """Canonical fields for request/proposal/action/ledger trace metadata."""
    return {
        "model_tier": decision.tier.value,
        "routed_model": decision.model,
        "routing": decision.to_dict(),
    }


class TieredStructuredActionGenerator:
    """One proof-agent protocol with cheap and strong execution tiers."""

    _is_structured_generator = True
    _uses_model = True

    def __init__(self, cheap_generator: Any, strong_generator: Any) -> None:
        self.cheap_generator = cheap_generator
        self.strong_generator = strong_generator

    def generate(self, request: ActionGenerationRequest):
        """Routing-off compatibility: use the cheap tier exactly."""
        return self.generate_for_route(
            request,
            RouteDecision(
                tier=ModelTier.CHEAP,
                model=getattr(getattr(self.cheap_generator, "config", None), "model", None),
                reason="routing_disabled",
                escalation_requested=False,
                escalation_granted=False,
            ),
        )

    def generate_for_route(
        self,
        request: ActionGenerationRequest,
        decision: RouteDecision,
    ):
        generator = (
            self.strong_generator
            if decision.tier is ModelTier.STRONG
            else self.cheap_generator
        )
        routed = []
        for proposal in generator.generate(request):
            routed.append(replace(
                proposal,
                metadata={**proposal.metadata, **routing_metadata(decision)},
            ))
        return tuple(routed)


def _escalation_reason(context: RoutingContext, config: ModelRouterConfig) -> str | None:
    if context.action_kind in config.strong_action_kinds:
        return "strong_action_kind"
    if context.cheap_failures_on_fingerprint >= config.cheap_failures_for_escalation:
        return "cheap_failures_same_fingerprint"
    if context.proposal_validation_failures >= config.validation_failures_for_escalation:
        return "proposal_validation_failures"
    if context.stalled_streak >= config.stalled_streak_for_escalation:
        return "stalled_repair"
    if (
        context.unlock_value >= config.min_unlock_value_for_escalation
        and context.cheap_success_rate is not None
        and context.cheap_success_rate <= config.max_cheap_success_rate_for_escalation
    ):
        return "high_unlock_low_cheap_success"
    return None
