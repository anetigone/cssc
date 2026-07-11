from __future__ import annotations

import unittest

from agent.search.budget import BudgetSnapshot
from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)
from agent.search.structured.action_frontier import CostEstimate, Estimate
from agent.search.structured.budget_hints import ObligationBudgetHint
from agent.search.structured.budget_snapshot import (
    ActionBudgetLimits,
    BudgetValueStatus,
    admit_estimate,
    build_unified_budget_snapshot,
)


def _budget() -> BudgetSnapshot:
    return BudgetSnapshot(
        checks_used=2, model_calls_used=1, elapsed_seconds=4.0,
        remaining_checks=3, remaining_model_calls=3,
    )


class UnifiedBudgetSnapshotTests(unittest.TestCase):
    def test_remaining_ratios_and_soft_context_are_explicit(self) -> None:
        snapshot = build_unified_budget_snapshot(
            _budget(), None,
            limits=ActionBudgetLimits(max_elapsed_seconds=10, global_reserve_checks=1),
            obligation_hints=(ObligationBudgetHint("root", 2, 3, borrowed_checks=1),),
        )
        self.assertEqual(snapshot.checks.remaining_ratio, 3 / 5)
        self.assertEqual(snapshot.model_requests.remaining_ratio, 3 / 4)
        self.assertEqual(snapshot.wall_time.remaining_ratio, 0.6)
        self.assertEqual(snapshot.obligations["root"].borrowed_checks, 1)
        self.assertEqual(snapshot.global_reserve_checks, 1)

    def test_unbounded_and_unknown_never_appear_as_one_hundred_percent(self) -> None:
        snapshot = build_unified_budget_snapshot(_budget(), None)
        self.assertEqual(snapshot.input_tokens.limit_status, BudgetValueStatus.UNBOUNDED)
        self.assertEqual(snapshot.input_tokens.spent_status, BudgetValueStatus.UNKNOWN)
        self.assertIsNone(snapshot.input_tokens.remaining_ratio)

    def test_provider_missing_usage_is_unknown_under_configured_limit(self) -> None:
        ledger = CostLedger((CostLedgerEvent(
            event_id="usage", kind=CostLedgerEventKind.PROVIDER_USAGE,
            scope=CostScope.PROPOSAL_GENERATION, status="completed", request_id="r1",
            input_tokens=CostMeasurement.unavailable("provider omitted usage"),
        ),))
        snapshot = build_unified_budget_snapshot(
            _budget(), ledger, limits=ActionBudgetLimits(max_input_tokens=100),
        )
        self.assertEqual(snapshot.input_tokens.limit_status, BudgetValueStatus.KNOWN)
        self.assertEqual(snapshot.input_tokens.spent_status, BudgetValueStatus.UNKNOWN)
        self.assertIsNone(snapshot.input_tokens.remaining_ratio)

    def test_known_hard_limit_rejects_but_unknown_dimension_is_not_compared(self) -> None:
        snapshot = build_unified_budget_snapshot(
            _budget(), None, limits=ActionBudgetLimits(max_input_tokens=100),
        )
        admission = admit_estimate(snapshot, CostEstimate(
            checks=Estimate(4), input_tokens=Estimate(200),
        ))
        self.assertFalse(admission.allowed)
        self.assertEqual(admission.rejected_dimensions, ("checks",))
        self.assertIn("input_tokens", admission.not_compared_dimensions)

    def test_same_snapshot_serves_proposal_and_execution_admission(self) -> None:
        snapshot = build_unified_budget_snapshot(_budget(), None)
        proposal = CostEstimate(model_requests=Estimate(1))
        execution = CostEstimate(checks=Estimate(1))
        self.assertTrue(admit_estimate(snapshot, proposal).allowed)
        self.assertTrue(admit_estimate(snapshot, execution).allowed)

    def test_checker_wall_milliseconds_are_compared_to_seconds(self) -> None:
        snapshot = build_unified_budget_snapshot(
            _budget(), None, limits=ActionBudgetLimits(max_elapsed_seconds=5),
        )
        # Four seconds elapsed leaves one second; 900ms fits, 1100ms does not.
        self.assertTrue(admit_estimate(
            snapshot, CostEstimate(checker_wall_ms=Estimate(900))
        ).allowed)
        self.assertFalse(admit_estimate(
            snapshot, CostEstimate(checker_wall_ms=Estimate(1100))
        ).allowed)


if __name__ == "__main__":
    unittest.main()
