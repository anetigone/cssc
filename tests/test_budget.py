from __future__ import annotations

import unittest

from agent.budget import BudgetConfig, BudgetExhausted, BudgetManager


class BudgetManagerTests(unittest.TestCase):
    def test_reserves_model_calls_and_checks(self) -> None:
        budget = BudgetManager(
            BudgetConfig(
                max_checks=2,
                max_model_calls=1,
                per_check_timeout_seconds=3.5,
            )
        )

        budget.reserve_model_call()
        first_slice = budget.reserve_check()
        second_slice = budget.reserve_check()

        self.assertEqual(first_slice.timeout_seconds, 3.5)
        self.assertEqual(second_slice.timeout_seconds, 3.5)
        self.assertEqual(budget.model_calls_used, 1)
        self.assertEqual(budget.checks_used, 2)
        self.assertFalse(budget.can_call_model())
        self.assertFalse(budget.can_check())
        self.assertEqual(budget.snapshot().exhausted_reason, "checks")

    def test_raises_when_check_budget_is_exhausted(self) -> None:
        budget = BudgetManager(BudgetConfig(max_checks=1, max_model_calls=2))

        budget.reserve_check()

        with self.assertRaises(BudgetExhausted):
            budget.reserve_check()


if __name__ == "__main__":
    unittest.main()
