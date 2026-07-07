from __future__ import annotations

import unittest

from agent.proof_system.base import CheckResult, DiagnosticCategory, GoalState, ParsedFeedback
from agent.search.execution import ExecutionMode
from agent.search.metrics import (
    attempt_metric,
    goal_fingerprint,
    goal_fingerprints,
    new_sample_id,
    run_metrics_payload,
    summarize_run,
)


class GoalFingerprintTests(unittest.TestCase):
    def test_normalizes_whitespace(self) -> None:
        self.assertEqual(
            goal_fingerprint("case h\n⊢ n + 0 = n"),
            goal_fingerprint("case h\n  ⊢ n + 0  = n\n"),
        )

    def test_preserves_order_and_multiplicity(self) -> None:
        fingerprints = goal_fingerprints(["⊢ A", "⊢ A", "", "⊢ B"])

        self.assertEqual(len(fingerprints), 3)
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertNotEqual(fingerprints[1], fingerprints[2])

    def test_empty_goal_has_no_fingerprint(self) -> None:
        self.assertEqual(goal_fingerprint("  \n"), "")


class AttemptMetricTests(unittest.TestCase):
    def test_records_raw_checker_observation(self) -> None:
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            unsolved_goals=("⊢ A", "⊢ A"),
            goal_state=(
                GoalState(text="⊢ A", goal_fingerprint="abc"),
                GoalState(text="sorry", goal_fingerprint="def", is_sorry_goal=True),
            ),
        )
        result = CheckResult(
            accepted=False,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="raw",
            parsed_feedback=feedback,
            elapsed_seconds=0.25,
        )

        metric = attempt_metric(3, action="model_complete", check_result=result)

        self.assertEqual(metric.attempt_index, 3)
        self.assertEqual(metric.category, "unsolved_goals")
        self.assertFalse(metric.accepted)
        self.assertEqual(len(metric.goal_fingerprints), 2)
        self.assertEqual(metric.goal_fingerprints[0], metric.goal_fingerprints[1])
        self.assertEqual(metric.elapsed_seconds, 0.25)
        # Structured goal state is recorded as raw counts, not interpreted.
        self.assertEqual(metric.metadata["goal_state_count"], 2)
        self.assertTrue(metric.metadata["has_sorry_goal"])

    def test_failure_without_goals_stays_an_empty_observation(self) -> None:
        result = CheckResult(
            accepted=False,
            category=DiagnosticCategory.PARSER_ERROR,
            raw_output="parser error",
            parsed_feedback=ParsedFeedback(
                category=DiagnosticCategory.PARSER_ERROR,
                message="parser error",
            ),
        )

        metric = attempt_metric(0, action="model_complete", check_result=result)

        self.assertEqual(metric.goal_fingerprints, ())
        self.assertFalse(metric.accepted)


class RunMetricsTests(unittest.TestCase):
    def test_rollup_contains_only_raw_run_facts(self) -> None:
        metrics = summarize_run(
            sample_id="sample-1",
            task_id="task-1",
            accepted=False,
            stop_reason="budget:checks",
            attempts=(),
            budget_checks_used=2,
            budget_model_calls_used=1,
            budget_exhausted_reason="checks",
            model_input_tokens=123,
            model_output_tokens=7,
        )

        payload = run_metrics_payload(metrics)

        self.assertEqual(payload["sample_id"], "sample-1")
        self.assertEqual(payload["task_id"], "task-1")
        self.assertEqual(payload["attempt_count"], 0)
        self.assertEqual(payload["model_input_tokens"], 123)
        self.assertEqual(payload["model_output_tokens"], 7)
        self.assertNotIn("progressed", payload)
        self.assertNotIn("pass_at_k", payload)
        self.assertNotIn("repeated_goal_stalls", payload)

    def test_sample_ids_are_unique(self) -> None:
        self.assertNotEqual(new_sample_id(), new_sample_id())

    def test_execution_mode_defaults_to_minimal(self) -> None:
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

        self.assertEqual(metrics.execution_mode, ExecutionMode.MINIMAL)
        self.assertEqual(run_metrics_payload(metrics)["execution_mode"], "minimal")

    def test_execution_mode_is_recorded_as_common_observation(self) -> None:
        metrics = summarize_run(
            sample_id="sample-2",
            task_id="task-1",
            accepted=False,
            stop_reason="budget:checks",
            attempts=(),
            budget_checks_used=0,
            budget_model_calls_used=0,
            budget_exhausted_reason=None,
            execution_mode=ExecutionMode.STRUCTURED,
        )

        self.assertEqual(metrics.execution_mode, ExecutionMode.STRUCTURED)
        self.assertEqual(run_metrics_payload(metrics)["execution_mode"], "structured")


if __name__ == "__main__":
    unittest.main()
