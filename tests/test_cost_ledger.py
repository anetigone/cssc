from __future__ import annotations

import unittest

from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
    MeasurementStatus,
    cost_ledger_from_dict,
)


class CostMeasurementTests(unittest.TestCase):
    def test_zero_is_distinct_from_unavailable_and_unbounded(self) -> None:
        self.assertEqual(CostMeasurement.observed(0).value, 0)
        self.assertEqual(CostMeasurement.observed(0).status, MeasurementStatus.OBSERVED)
        self.assertIsNone(CostMeasurement.unavailable("provider omitted usage").value)
        self.assertEqual(CostMeasurement.unbounded().status, MeasurementStatus.UNBOUNDED)

    def test_missing_value_requires_explicit_status_and_reason(self) -> None:
        with self.assertRaises(ValueError):
            CostMeasurement(None, MeasurementStatus.OBSERVED)
        with self.assertRaises(ValueError):
            CostMeasurement(None, MeasurementStatus.UNAVAILABLE)


class CostLedgerReconciliationTests(unittest.TestCase):
    def _provider_usage(self, event_id: str, *, input_tokens: CostMeasurement | None) -> CostLedgerEvent:
        return CostLedgerEvent(
            event_id=event_id,
            kind=CostLedgerEventKind.PROVIDER_USAGE,
            scope=CostScope.PROPOSAL_GENERATION,
            status="completed",
            request_id="request-1",
            input_tokens=input_tokens,
            output_tokens=CostMeasurement.observed(3),
            billed_tokens=CostMeasurement.observed(8),
        )

    def test_zero_is_preserved_in_totals(self) -> None:
        ledger = CostLedger((self._provider_usage("usage-1", input_tokens=CostMeasurement.observed(0)),))
        total = ledger.reconcile().totals["input_tokens"].measurement
        self.assertEqual(total.value, 0)
        self.assertEqual(total.status, MeasurementStatus.OBSERVED)

    def test_missing_usage_makes_sum_na_not_zero(self) -> None:
        ledger = CostLedger((self._provider_usage("usage-1", input_tokens=CostMeasurement.unavailable("provider omitted usage")),))
        total = ledger.reconcile().totals["input_tokens"].measurement
        self.assertIsNone(total.value)
        self.assertEqual(total.status, MeasurementStatus.UNAVAILABLE)

    def test_unbounded_is_not_unknown(self) -> None:
        event = CostLedgerEvent(
            event_id="checker-1", kind=CostLedgerEventKind.CHECKER,
            scope=CostScope.TOOL_CHECK, status="completed", checker_kind="candidate",
            wall_time_ms=CostMeasurement.unbounded("no checker time limit"),
        )
        measurement = CostLedger((event,)).reconcile().totals["checker_wall_time_ms"].measurement
        self.assertEqual(measurement.status, MeasurementStatus.UNBOUNDED)
        self.assertEqual(measurement.reason, "no checker time limit")

    def test_retry_and_shared_batch_usage_are_each_counted_once(self) -> None:
        request = CostLedgerEvent(
            event_id="request-1", kind=CostLedgerEventKind.PROVIDER_REQUEST,
            scope=CostScope.RETRY, status="failed", request_id="batch-7", model="cheap",
            wall_time_ms=CostMeasurement.observed(11),
        )
        usage = self._provider_usage("usage-1", input_tokens=CostMeasurement.observed(7))
        ledger = CostLedger((request, usage))
        report = ledger.reconcile()
        self.assertEqual(report.totals["input_tokens"].measurement.value, 7)
        self.assertEqual(report.scope_event_counts["retry"], 1)
        self.assertTrue(report.reconciled)

    def test_duplicate_usage_for_shared_batch_is_not_double_counted(self) -> None:
        first = self._provider_usage(
            "usage-1", input_tokens=CostMeasurement.observed(7)
        )
        duplicate = self._provider_usage(
            "usage-2", input_tokens=CostMeasurement.observed(7)
        )
        report = CostLedger((first, duplicate)).reconcile()
        self.assertEqual(report.totals["input_tokens"].measurement.value, 7)
        self.assertFalse(report.reconciled)
        self.assertEqual(report.unallocated_event_ids, ("usage-2",))

    def test_assembly_checker_is_included_as_fixed_cost(self) -> None:
        event = CostLedgerEvent(
            event_id="assembly-1", kind=CostLedgerEventKind.CHECKER,
            scope=CostScope.ASSEMBLY, status="completed", checker_kind="assembly",
            wall_time_ms=CostMeasurement.observed(21), cpu_time_ms=CostMeasurement.observed(13),
        )
        report = CostLedger((event,)).reconcile()
        self.assertEqual(report.totals["checker_wall_time_ms"].measurement.value, 21)
        self.assertEqual(report.totals["checker_cpu_time_ms"].measurement.value, 13)
        self.assertEqual(report.scope_event_counts["assembly"], 1)

    def test_round_trip_and_legacy_absence(self) -> None:
        ledger = CostLedger((self._provider_usage("usage-1", input_tokens=CostMeasurement.estimated(5)),))
        self.assertEqual(cost_ledger_from_dict(ledger.to_dict()), ledger)
        legacy = cost_ledger_from_dict(None).reconcile().totals["input_tokens"].measurement
        self.assertEqual(legacy.status, MeasurementStatus.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
