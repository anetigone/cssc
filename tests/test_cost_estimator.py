from __future__ import annotations

import unittest

from agent.proof_system.workspace import SearchActionKind
from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)
from agent.search.structured.action_frontier import CostEstimate, Estimate
from agent.search.structured.cost_estimator import (
    ActionCostEstimator,
    CompletedActionCost,
    CostBucket,
    CostHistorySnapshot,
    cost_history_snapshot_from_dict,
    cost_history_snapshot_fingerprint,
)


def _bucket(*, kind: SearchActionKind = SearchActionKind.IMPLEMENT, tier: str = "cheap") -> CostBucket:
    return CostBucket(
        model="model-a", model_tier=tier, action_kind=kind,
        imports_profile="mathlib", goal_size_bucket="small",
        obligation_size_bucket="small", repair_state="fresh", stalled=False,
    )


def _sample(action_id: str, bucket: CostBucket, *, checks: float, tokens: float | None = 10) -> CompletedActionCost:
    return CompletedActionCost(
        action_id=action_id, bucket=bucket,
        actual=CostEstimate(
            checks=Estimate(checks),
            input_tokens=Estimate(tokens) if tokens is not None else None,
            sample_count=1, source="history",
        ),
        completed_event_ids=(f"event:{action_id}",),
    )


class ActionCostEstimatorTests(unittest.TestCase):
    def test_uses_median_of_frozen_matching_history(self) -> None:
        bucket = _bucket()
        snapshot = CostHistorySnapshot("pilot-1", (
            _sample("a", bucket, checks=1, tokens=10),
            _sample("b", bucket, checks=3, tokens=30),
            _sample("c", bucket, checks=2, tokens=20),
        ))
        result = ActionCostEstimator(snapshot).estimate(bucket)
        self.assertEqual(result.estimate.source, "history")
        self.assertEqual(result.estimate.checks, Estimate(2))
        self.assertEqual(result.estimate.input_tokens, Estimate(20))
        self.assertIsNone(result.fallback_reason)

    def test_insufficient_samples_use_frozen_prior(self) -> None:
        bucket = _bucket()
        snapshot = CostHistorySnapshot("pilot-1", (_sample("a", bucket, checks=9),))
        result = ActionCostEstimator(snapshot, min_samples=2).estimate(bucket)
        self.assertEqual(result.estimate.source, "prior")
        self.assertEqual(result.estimate.checks, Estimate(1))
        self.assertEqual(result.matching_sample_count, 1)
        self.assertEqual(result.fallback_reason, "insufficient_matching_samples")

    def test_missing_dimension_does_not_enter_error_or_median(self) -> None:
        bucket = _bucket()
        snapshot = CostHistorySnapshot("pilot-1", (
            _sample("a", bucket, checks=1, tokens=None),
            _sample("b", bucket, checks=1, tokens=None),
            _sample("c", bucket, checks=1, tokens=None),
        ))
        estimator = ActionCostEstimator(snapshot)
        self.assertIsNone(estimator.estimate(bucket).estimate.input_tokens)
        report = estimator.calibration_report((_sample("held", bucket, checks=1, tokens=None),))
        self.assertEqual(report.coverage["input_tokens"], 0.0)
        self.assertIsNone(report.median_absolute_error["input_tokens"])

    def test_different_frozen_snapshots_can_change_estimate(self) -> None:
        bucket = _bucket()
        low = CostHistorySnapshot("low", tuple(_sample(str(i), bucket, checks=1) for i in range(3)))
        high = CostHistorySnapshot("high", tuple(_sample(str(i), bucket, checks=4) for i in range(3)))
        self.assertNotEqual(
            ActionCostEstimator(low).estimate(bucket).estimate.checks,
            ActionCostEstimator(high).estimate(bucket).estimate.checks,
        )

    def test_snapshot_round_trip_preserves_versioned_history(self) -> None:
        bucket = _bucket()
        snapshot = CostHistorySnapshot("pilot-1", (_sample("a", bucket, checks=1),))
        self.assertEqual(cost_history_snapshot_from_dict(snapshot.to_dict()), snapshot)
        self.assertEqual(
            cost_history_snapshot_fingerprint(snapshot),
            cost_history_snapshot_fingerprint(
                cost_history_snapshot_from_dict(snapshot.to_dict())
            ),
        )

    def test_snapshot_fingerprint_changes_with_content(self) -> None:
        bucket = _bucket()
        low = CostHistorySnapshot("same-id", (_sample("a", bucket, checks=1),))
        high = CostHistorySnapshot("same-id", (_sample("a", bucket, checks=2),))
        self.assertNotEqual(
            cost_history_snapshot_fingerprint(low),
            cost_history_snapshot_fingerprint(high),
        )


class LedgerSnapshotTests(unittest.TestCase):
    def test_only_completed_events_become_history(self) -> None:
        bucket = _bucket()
        completed = CostLedgerEvent(
            event_id="check-ok", kind=CostLedgerEventKind.CHECKER,
            scope=CostScope.EXECUTION, status="completed", checker_kind="candidate",
            wall_time_ms=CostMeasurement.observed(12), metadata={"action_id": "ok"},
        )
        failed = CostLedgerEvent(
            event_id="check-failed", kind=CostLedgerEventKind.CHECKER,
            scope=CostScope.EXECUTION, status="failed", checker_kind="candidate",
            metadata={"action_id": "incomplete"},
        )
        snapshot = CostHistorySnapshot.from_completed_ledger(
            CostLedger((completed, failed)), snapshot_id="pilot", buckets_by_action_id={"ok": bucket, "incomplete": bucket},
        )
        self.assertEqual([sample.action_id for sample in snapshot.samples], ["ok"])
        self.assertEqual(snapshot.samples[0].actual.checker_wall_ms, Estimate(12))

    def test_provider_request_without_usage_keeps_tokens_unavailable(self) -> None:
        bucket = _bucket()
        request = CostLedgerEvent(
            event_id="request", kind=CostLedgerEventKind.PROVIDER_REQUEST,
            scope=CostScope.PROPOSAL_GENERATION, status="completed",
            request_id="r1", metadata={"action_id": "a"},
        )
        snapshot = CostHistorySnapshot.from_completed_ledger(
            CostLedger((request,)), snapshot_id="pilot",
            buckets_by_action_id={"a": bucket},
        )
        self.assertIsNone(snapshot.samples[0].actual.input_tokens)


if __name__ == "__main__":
    unittest.main()
