from __future__ import annotations

import unittest

from agent.proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
)
from agent.search.metrics import (
    AttemptMetric,
    feedback_goal_fingerprint,
    goal_fingerprint,
    goal_fingerprints,
    is_stall_category,
    normalize_goal_text,
    run_metrics_payload,
    summarize_run,
)


class GoalFingerprintTests(unittest.TestCase):
    def test_normalizes_whitespace_so_same_goal_is_stable(self) -> None:
        goal_a = "case h\n⊢ n + 0 = n"
        goal_b = "case h\n  ⊢ n + 0  = n\n\n"

        self.assertEqual(goal_fingerprint(goal_a), goal_fingerprint(goal_b))
        self.assertTrue(goal_fingerprint(goal_a))

    def test_distinct_goals_get_distinct_fingerprints(self) -> None:
        self.assertNotEqual(
            goal_fingerprint("⊢ n + 0 = n"),
            goal_fingerprint("⊢ n * 1 = n"),
        )

    def test_empty_goal_has_no_fingerprint(self) -> None:
        self.assertEqual(goal_fingerprint(""), "")
        self.assertEqual(goal_fingerprint("   \n  "), "")

    def test_fingerprints_preserve_order_and_drop_empties(self) -> None:
        fingerprints = goal_fingerprints(["⊢ a", "", "  ", "⊢ b"])

        self.assertEqual(len(fingerprints), 2)

    def test_feedback_fingerprint_uses_first_unsolved_goal(self) -> None:
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            unsolved_goals=("⊢ n + 0 = n", "⊢ n * 1 = n"),
        )

        self.assertEqual(
            feedback_goal_fingerprint(feedback),
            goal_fingerprint("⊢ n + 0 = n"),
        )

    def test_feedback_fingerprint_is_none_without_goals(self) -> None:
        self.assertIsNone(feedback_goal_fingerprint(None))
        self.assertIsNone(
            feedback_goal_fingerprint(
                ParsedFeedback(category=DiagnosticCategory.PROOF_ACCEPTED)
            )
        )


class RunMetricsTests(unittest.TestCase):
    def test_counts_repeated_stalls_on_same_goal(self) -> None:
        same_goal = "⊢ n + 0 = n"
        fingerprint = goal_fingerprint(same_goal)
        attempts = (
            AttemptMetric(
                attempt_index=0,
                action="model_complete",
                category="unsolved_goals",
                accepted=False,
                goal_fingerprint=fingerprint,
                error_message="unsolved goals",
                progressed=False,
                elapsed_seconds=0.1,
            ),
            AttemptMetric(
                attempt_index=1,
                action="model_complete",
                category="unsolved_goals",
                accepted=False,
                goal_fingerprint=fingerprint,
                error_message="unsolved goals",
                progressed=False,
                elapsed_seconds=0.1,
            ),
            AttemptMetric(
                attempt_index=2,
                action="model_complete",
                category="unsolved_goals",
                accepted=False,
                goal_fingerprint=goal_fingerprint("⊢ m + 0 = m"),
                error_message="unsolved goals",
                progressed=False,
                elapsed_seconds=0.1,
            ),
        )

        metrics = summarize_run(
            accepted=False,
            stop_reason="budget:model_calls",
            attempts=attempts,
            pass_at_k=1,
            budget_checks_used=3,
            budget_model_calls_used=3,
            budget_exhausted_reason="model_calls",
        )

        self.assertFalse(metrics.accepted)
        self.assertEqual(metrics.stop_reason, "budget:model_calls")
        self.assertEqual(metrics.distinct_goal_fingerprints, 2)
        self.assertEqual(metrics.repeated_goal_stalls, 1)

    def test_accepted_run_has_no_stalls(self) -> None:
        attempts = (
            AttemptMetric(
                attempt_index=0,
                action="model_complete",
                category="proof_accepted",
                accepted=True,
                goal_fingerprint=None,
                error_message="",
                progressed=True,
                elapsed_seconds=0.1,
            ),
        )

        metrics = summarize_run(
            accepted=True,
            stop_reason="accepted",
            attempts=attempts,
            pass_at_k=1,
            budget_checks_used=1,
            budget_model_calls_used=1,
            budget_exhausted_reason=None,
        )

        self.assertTrue(metrics.accepted)
        self.assertEqual(metrics.repeated_goal_stalls, 0)
        self.assertEqual(metrics.distinct_goal_fingerprints, 0)

    def test_pass_at_k_defaults_to_one_and_is_floored(self) -> None:
        metrics = summarize_run(
            accepted=False,
            stop_reason="no_actions",
            attempts=(),
            budget_checks_used=0,
            budget_model_calls_used=1,
            budget_exhausted_reason=None,
        )

        self.assertEqual(metrics.pass_at_k, 1)

    def test_payload_round_trips_metric_fields(self) -> None:
        metrics = summarize_run(
            accepted=False,
            stop_reason="budget:checks",
            attempts=(
                AttemptMetric(
                    attempt_index=0,
                    action="model_complete",
                    category="unsolved_goals",
                    accepted=False,
                    goal_fingerprint="abc123",
                    error_message="unsolved goals",
                    progressed=False,
                    elapsed_seconds=0.2,
                ),
            ),
            budget_checks_used=1,
            budget_model_calls_used=1,
            budget_exhausted_reason="checks",
        )

        payload = run_metrics_payload(metrics)

        self.assertEqual(payload["stop_reason"], "budget:checks")
        self.assertEqual(payload["attempt_count"], 1)
        self.assertEqual(payload["attempts"][0]["goal_fingerprint"], "abc123")


class IsStallCategoryTests(unittest.TestCase):
    def test_stall_categories(self) -> None:
        self.assertTrue(is_stall_category("unsolved_goals"))
        self.assertTrue(is_stall_category("tactic_failed"))
        self.assertTrue(is_stall_category("type_mismatch"))

    def test_non_stall_categories(self) -> None:
        self.assertFalse(is_stall_category("parser_error"))
        self.assertFalse(is_stall_category("timeout"))
        self.assertFalse(is_stall_category("proof_accepted"))


if __name__ == "__main__":
    unittest.main()
