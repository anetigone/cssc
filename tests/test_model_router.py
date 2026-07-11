from __future__ import annotations

import unittest

from agent.proof_system.workspace import SearchActionKind
from agent.search.budget import BudgetSnapshot
from agent.search.structured.action_frontier import CostEstimate, Estimate
from agent.search.structured.budget_snapshot import build_unified_budget_snapshot
from agent.search.structured.model_router import (
    ModelRouterConfig,
    ModelTier,
    RoutingContext,
    route_model,
    routing_metadata,
    TieredStructuredActionGenerator,
)


def _budget(*, remaining_checks: int = 3, remaining_models: int = 3):
    return build_unified_budget_snapshot(BudgetSnapshot(
        checks_used=0, model_calls_used=0, elapsed_seconds=0,
        remaining_checks=remaining_checks, remaining_model_calls=remaining_models,
    ), None)


class ModelRouterTests(unittest.TestCase):
    def test_routing_off_is_strict_single_cheap_policy(self) -> None:
        decision = route_model(
            RoutingContext(SearchActionKind.DECOMPOSE, stalled_streak=9), _budget(),
        )
        self.assertEqual(decision.tier, ModelTier.CHEAP)
        self.assertEqual(decision.reason, "routing_disabled")
        self.assertFalse(decision.escalation_requested)

    def test_stalled_cheap_failures_escalate_when_budget_allows(self) -> None:
        decision = route_model(
            RoutingContext(
                SearchActionKind.REPAIR_IMPLEMENTATION,
                cheap_failures_on_fingerprint=2,
            ),
            _budget(),
            config=ModelRouterConfig(enabled=True, cheap_model="small", strong_model="large"),
        )
        self.assertEqual(decision.tier, ModelTier.STRONG)
        self.assertEqual(decision.model, "large")
        self.assertEqual(decision.reason, "cheap_failures_same_fingerprint")
        self.assertTrue(decision.escalation_granted)

    def test_budget_rejects_strong_escalation(self) -> None:
        decision = route_model(
            RoutingContext(SearchActionKind.DECOMPOSE), _budget(remaining_checks=1),
            config=ModelRouterConfig(
                enabled=True,
                strong_cost=CostEstimate(model_requests=Estimate(1), checks=Estimate(2)),
            ),
        )
        self.assertEqual(decision.tier, ModelTier.CHEAP)
        self.assertEqual(decision.reason, "strong_budget_rejected:strong_action_kind")
        self.assertTrue(decision.escalation_requested)
        self.assertFalse(decision.escalation_granted)
        self.assertIn("checks", decision.budget_admission.rejected_dimensions)

    def test_trusted_cheap_cache_and_capability_probe_do_not_escalate(self) -> None:
        config = ModelRouterConfig(enabled=True)
        cached = route_model(
            RoutingContext(SearchActionKind.DECOMPOSE, has_trusted_cheap_cached_action=True), _budget(), config=config,
        )
        probe = route_model(
            RoutingContext(SearchActionKind.RUN_CAPABILITY_TEST, is_low_cost_capability_probe=True), _budget(), config=config,
        )
        self.assertEqual(cached.reason, "trusted_cheap_cache")
        self.assertEqual(probe.reason, "low_cost_capability_probe")
        self.assertEqual(cached.tier, ModelTier.CHEAP)
        self.assertEqual(probe.tier, ModelTier.CHEAP)

    def test_metadata_carries_model_and_tier_for_all_trace_surfaces(self) -> None:
        decision = route_model(
            RoutingContext(SearchActionKind.DECOMPOSE), _budget(),
            config=ModelRouterConfig(enabled=True, cheap_model="small", strong_model="large"),
        )
        metadata = routing_metadata(decision)
        self.assertEqual(metadata["model_tier"], "strong")
        self.assertEqual(metadata["routed_model"], "large")
        self.assertTrue(metadata["routing"]["escalation_granted"])

    def test_unknown_spend_under_configured_limit_rejects_strong(self) -> None:
        from agent.search.structured.budget_snapshot import ActionBudgetLimits

        budget = build_unified_budget_snapshot(
            BudgetSnapshot(
                checks_used=0, model_calls_used=0, elapsed_seconds=0,
                remaining_checks=3, remaining_model_calls=3,
            ),
            None,
            limits=ActionBudgetLimits(max_input_tokens=100),
        )
        decision = route_model(
            RoutingContext(SearchActionKind.DECOMPOSE), budget,
            config=ModelRouterConfig(
                enabled=True,
                strong_cost=CostEstimate(
                    model_requests=Estimate(1), input_tokens=Estimate(20)
                ),
            ),
        )
        self.assertFalse(decision.escalation_granted)
        self.assertIn("input_tokens", decision.budget_admission.rejected_dimensions)

    def test_tiered_generator_executes_selected_tier_and_tags_proposal(self) -> None:
        from agent.proof_system.base import ProofTask
        from agent.proof_system.workspace import SearchAction
        from agent.search.action import ActionGenerationRequest
        from agent.search.structured.proposal import (
            ImplementPayload,
            StructuredActionProposal,
        )

        class Generator:
            def __init__(self, label: str) -> None:
                self.label = label
                self.calls = 0

            def generate(self, request):
                self.calls += 1
                return (StructuredActionProposal(
                    action=SearchAction(
                        SearchActionKind.IMPLEMENT,
                        request.metadata["branch_id"],
                        rationale="implement",
                    ),
                    payload=ImplementPayload(self.label),
                ),)

        cheap = Generator("cheap")
        strong = Generator("strong")
        tiered = TieredStructuredActionGenerator(cheap, strong)
        request = ActionGenerationRequest(
            task=ProofTask("t", "theorem t : True := by {{proof}}"),
            attempt_index=0,
            metadata={"branch_id": "b"},
        )
        decision = route_model(
            RoutingContext(SearchActionKind.DECOMPOSE), _budget(),
            config=ModelRouterConfig(enabled=True, strong_model="large"),
        )
        proposal = tiered.generate_for_route(request, decision)[0]
        self.assertEqual(cheap.calls, 0)
        self.assertEqual(strong.calls, 1)
        self.assertEqual(proposal.payload.proof_text, "strong")
        self.assertEqual(proposal.metadata["model_tier"], "strong")


if __name__ == "__main__":
    unittest.main()
