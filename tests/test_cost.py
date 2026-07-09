from __future__ import annotations

import unittest

from agent.search.budget import BudgetSnapshot
from agent.search.cost import (
    CostVector,
    add_cost,
    cost_vector_from_attempt,
    cost_vector_from_dict,
    cost_vector_from_metrics_and_budget,
    to_dict,
    zero_cost,
)
from agent.search.metrics import AttemptMetric, summarize_run


class CostVectorSerializationTests(unittest.TestCase):
    def test_defaults_are_zero(self) -> None:
        cost = CostVector()
        self.assertEqual(to_dict(cost), {
            "model_calls": 0,
            "checks": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "elapsed_ms": 0,
        })

    def test_round_trip_preserves_all_fields(self) -> None:
        cost = CostVector(
            model_calls=3,
            checks=7,
            input_tokens=412,
            output_tokens=58,
            elapsed_ms=9500,
        )
        restored = cost_vector_from_dict(to_dict(cost))
        self.assertEqual(restored, cost)

    def test_from_dict_is_lenient_about_missing_keys(self) -> None:
        restored = cost_vector_from_dict({})
        self.assertEqual(restored, zero_cost())

    def test_from_dict_coerces_to_int(self) -> None:
        restored = cost_vector_from_dict({"checks": "5", "elapsed_ms": "100"})
        self.assertEqual(restored.checks, 5)
        self.assertEqual(restored.elapsed_ms, 100)


class AddCostTests(unittest.TestCase):
    def test_adds_each_field(self) -> None:
        left = CostVector(model_calls=1, checks=2, input_tokens=10, output_tokens=3, elapsed_ms=500)
        right = CostVector(model_calls=4, checks=5, input_tokens=20, output_tokens=7, elapsed_ms=1500)
        self.assertEqual(
            add_cost(left, right),
            CostVector(model_calls=5, checks=7, input_tokens=30, output_tokens=10, elapsed_ms=2000),
        )

    def test_zero_is_identity(self) -> None:
        cost = CostVector(model_calls=2, checks=9, input_tokens=4, output_tokens=1, elapsed_ms=300)
        self.assertEqual(add_cost(cost, zero_cost()), cost)

    def test_add_is_commutative(self) -> None:
        a = CostVector(model_calls=1, checks=2, input_tokens=3, output_tokens=4, elapsed_ms=5)
        b = CostVector(model_calls=6, checks=7, input_tokens=8, output_tokens=9, elapsed_ms=10)
        self.assertEqual(add_cost(a, b), add_cost(b, a))

    def test_does_not_mutate_operands(self) -> None:
        a = CostVector(model_calls=1, checks=1)
        add_cost(a, a)
        self.assertEqual(a.model_calls, 1)
        self.assertEqual(a.checks, 1)


class DeriveFromMetricsAndBudgetTests(unittest.TestCase):
    def _snapshot(self, elapsed_seconds: float = 2.5) -> BudgetSnapshot:
        return BudgetSnapshot(
            checks_used=8,
            model_calls_used=4,
            elapsed_seconds=elapsed_seconds,
            remaining_checks=0,
            remaining_model_calls=0,
            exhausted_reason=None,
        )

    def test_projects_run_facts_without_interpreting(self) -> None:
        metrics = summarize_run(
            sample_id="sample-1",
            task_id="task-1",
            accepted=False,
            stop_reason="budget:checks",
            attempts=(),
            budget_checks_used=8,
            budget_model_calls_used=4,
            budget_exhausted_reason="checks",
            model_input_tokens=412,
            model_output_tokens=58,
        )
        cost = cost_vector_from_metrics_and_budget(metrics, self._snapshot(2.5))
        self.assertEqual(cost.model_calls, 4)
        self.assertEqual(cost.checks, 8)
        self.assertEqual(cost.input_tokens, 412)
        self.assertEqual(cost.output_tokens, 58)
        # elapsed_ms comes from the budget wall-clock, not per-check sums.
        self.assertEqual(cost.elapsed_ms, 2500)

    def test_elapsed_ms_rounds_to_nearest_millisecond(self) -> None:
        metrics = summarize_run(
            sample_id="sample-1",
            task_id="task-1",
            accepted=False,
            stop_reason="budget:checks",
            attempts=(),
            budget_checks_used=0,
            budget_model_calls_used=0,
            budget_exhausted_reason=None,
        )
        cost = cost_vector_from_metrics_and_budget(metrics, self._snapshot(0.0014))
        self.assertEqual(cost.elapsed_ms, 1)

    def test_handles_missing_metrics(self) -> None:
        cost = cost_vector_from_metrics_and_budget(None, self._snapshot(1.0))
        self.assertEqual(cost, CostVector(elapsed_ms=1000))


class DeriveFromAttemptTests(unittest.TestCase):
    def test_only_records_check_wall_clock(self) -> None:
        attempt = AttemptMetric(
            attempt_index=2,
            action="model_complete",
            category="unsolved_goals",
            accepted=False,
            goal_fingerprints=(),
            error_message="",
            elapsed_seconds=0.25,
        )
        cost = cost_vector_from_attempt(attempt)
        # Attempts carry no run-level token or call counters.
        self.assertEqual(cost, CostVector(elapsed_ms=250))


if __name__ == "__main__":
    unittest.main()
