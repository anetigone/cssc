from __future__ import annotations

import unittest

from agent.proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
)
from agent.search.metrics import (
    AttemptMetric,
    EvaluationAggregator,
    GoalSetDelta,
    GoalSetSnapshot,
    compare_goal_sets,
    goal_fingerprint,
    goal_fingerprints,
    is_stall_category,
    new_sample_id,
    run_metrics_payload,
    summarize_run,
)


def _metric(
    index: int,
    *,
    goals: tuple[str, ...] = (),
    accepted: bool = False,
    action: str = "model_complete",
    category: str = "unsolved_goals",
    elapsed: float = 0.1,
) -> AttemptMetric:
    """Build an attempt metric with a goal set, for delta-aware tests."""
    snapshot = GoalSetSnapshot.from_goals(goals)
    return AttemptMetric(
        attempt_index=index,
        action=action,
        category=category,
        accepted=accepted,
        goal_snapshot=snapshot,
        goal_delta=GoalSetDelta(
            solved=frozenset(),
            retained=frozenset(),
            introduced=snapshot.fingerprint_set,
            goal_count_delta=len(snapshot.fingerprints),
        ),
        progressed=accepted,
        error_message="unsolved goals" if not accepted else "",
        elapsed_seconds=elapsed,
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

    def test_snapshot_from_feedback_fingerprints_all_goals(self) -> None:
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            unsolved_goals=("⊢ n + 0 = n", "⊢ n * 1 = n"),
        )

        snapshot = GoalSetSnapshot.from_feedback(feedback)

        self.assertEqual(
            snapshot.fingerprints,
            (goal_fingerprint("⊢ n + 0 = n"), goal_fingerprint("⊢ n * 1 = n")),
        )

    def test_snapshot_from_feedback_without_goals_is_empty(self) -> None:
        self.assertEqual(
            GoalSetSnapshot.from_feedback(None).fingerprints,
            (),
        )
        self.assertEqual(
            GoalSetSnapshot.from_feedback(
                ParsedFeedback(category=DiagnosticCategory.PROOF_ACCEPTED)
            ).fingerprints,
            (),
        )


class GoalSetDeltaTests(unittest.TestCase):
    def test_progress_requires_discharging_a_goal_without_reintroducing(self) -> None:
        # attempt 0 has goals {A, B}; attempt 1 keeps {B} (A solved, nothing
        # introduced) -> forward progress.
        parent = GoalSetSnapshot.from_goals(["⊢ A", "⊢ B"])
        child = GoalSetSnapshot.from_goals(["⊢ B"])

        delta = compare_goal_sets(parent, child)

        self.assertTrue(delta.made_progress)
        self.assertEqual(delta.goal_count_delta, -1)

    def test_repeated_failure_on_same_goal_is_not_progress(self) -> None:
        # attempt 0 and attempt 1 both have {A}: nothing solved, nothing
        # introduced -> not progress.
        snapshot = GoalSetSnapshot.from_goals(["⊢ A"])

        delta = compare_goal_sets(snapshot, snapshot)

        self.assertFalse(delta.made_progress)
        self.assertEqual(delta.retained, snapshot.fingerprint_set)

    def test_goal_swapped_is_not_progress(self) -> None:
        # {A, B} -> {B, C}: A solved but C introduced -> net wash, not progress.
        parent = GoalSetSnapshot.from_goals(["⊢ A", "⊢ B"])
        child = GoalSetSnapshot.from_goals(["⊢ B", "⊢ C"])

        delta = compare_goal_sets(parent, child)

        self.assertFalse(delta.made_progress)
        self.assertIn(next(iter(parent.fingerprint_set - child.fingerprint_set)), delta.solved)
        self.assertEqual(delta.introduced, child.fingerprint_set - parent.fingerprint_set)

    def test_first_attempt_introduces_everything(self) -> None:
        child = GoalSetSnapshot.from_goals(["⊢ A"])

        delta = compare_goal_sets(None, child)

        self.assertFalse(delta.made_progress)
        self.assertEqual(delta.introduced, child.fingerprint_set)


class RunMetricsTests(unittest.TestCase):
    def test_counts_repeated_stalls_on_same_goal(self) -> None:
        same = goal_fingerprint("⊢ A")
        snapshot = GoalSetSnapshot(fingerprints=(same,), fingerprint_set=frozenset({same}))
        attempts = (
            _metric(0, goals=("⊢ A",)),
            AttemptMetric(
                attempt_index=1,
                action="model_complete",
                category="unsolved_goals",
                accepted=False,
                goal_snapshot=snapshot,
                goal_delta=GoalSetDelta(
                    solved=frozenset(),
                    retained=frozenset({same}),
                    introduced=frozenset(),
                    goal_count_delta=0,
                ),
                progressed=False,
                error_message="unsolved goals",
                elapsed_seconds=0.1,
            ),
        )

        metrics = summarize_run(
            sample_id="s1",
            task_id="t",
            accepted=False,
            stop_reason="budget:model_calls",
            attempts=attempts,
            budget_checks_used=2,
            budget_model_calls_used=2,
            budget_exhausted_reason="model_calls",
        )

        self.assertFalse(metrics.accepted)
        self.assertEqual(metrics.repeated_goal_stalls, 1)
        self.assertEqual(metrics.distinct_goal_fingerprints, 1)

    def test_accepted_run_has_no_stalls(self) -> None:
        attempts = (_metric(0, accepted=True, category="proof_accepted"),)

        metrics = summarize_run(
            sample_id="s1",
            task_id="t",
            accepted=True,
            stop_reason="accepted",
            attempts=attempts,
            budget_checks_used=1,
            budget_model_calls_used=1,
            budget_exhausted_reason=None,
        )

        self.assertTrue(metrics.accepted)
        self.assertEqual(metrics.repeated_goal_stalls, 0)

    def test_sample_id_is_unique_per_call(self) -> None:
        self.assertNotEqual(new_sample_id(), new_sample_id())

    def test_payload_round_trips_full_goal_set(self) -> None:
        metric = _metric(0, goals=("⊢ A", "⊢ B"))
        metrics = summarize_run(
            sample_id="s9",
            task_id="t9",
            accepted=False,
            stop_reason="budget:checks",
            attempts=(metric,),
            budget_checks_used=1,
            budget_model_calls_used=1,
            budget_exhausted_reason="checks",
        )

        payload = run_metrics_payload(metrics)

        self.assertEqual(payload["sample_id"], "s9")
        self.assertEqual(len(payload["attempts"][0]["goal_fingerprints"]), 2)


class EvaluationAggregatorTests(unittest.TestCase):
    def test_pass_at_k_is_one_if_any_sample_succeeds(self) -> None:
        samples = (
            summarize_run(
                sample_id="a",
                task_id="t",
                accepted=False,
                stop_reason="budget:checks",
                attempts=(),
                budget_checks_used=1,
                budget_model_calls_used=1,
                budget_exhausted_reason="checks",
            ),
            summarize_run(
                sample_id="b",
                task_id="t",
                accepted=True,
                stop_reason="accepted",
                attempts=(),
                budget_checks_used=1,
                budget_model_calls_used=1,
                budget_exhausted_reason=None,
            ),
        )

        result = EvaluationAggregator().pass_at_k(samples)

        self.assertIsNotNone(result)
        assert result is not None  # for type checkers
        self.assertEqual(result.k, 2)
        self.assertEqual(result.successes, 1)
        self.assertEqual(result.pass_at_k, 1.0)

    def test_pass_at_k_is_zero_when_all_fail(self) -> None:
        samples = (
            summarize_run(
                sample_id="a",
                task_id="t",
                accepted=False,
                stop_reason="budget:checks",
                attempts=(),
                budget_checks_used=1,
                budget_model_calls_used=1,
                budget_exhausted_reason="checks",
            ),
        )

        result = EvaluationAggregator().pass_at_k(samples)

        assert result is not None
        self.assertEqual(result.pass_at_k, 0.0)

    def test_aggregator_rejects_mixed_tasks(self) -> None:
        samples = (
            summarize_run(
                sample_id="a",
                task_id="t1",
                accepted=True,
                stop_reason="accepted",
                attempts=(),
                budget_checks_used=1,
                budget_model_calls_used=1,
                budget_exhausted_reason=None,
            ),
            summarize_run(
                sample_id="b",
                task_id="t2",
                accepted=True,
                stop_reason="accepted",
                attempts=(),
                budget_checks_used=1,
                budget_model_calls_used=1,
                budget_exhausted_reason=None,
            ),
        )

        with self.assertRaises(ValueError):
            EvaluationAggregator().pass_at_k(samples)

    def test_empty_samples_returns_none(self) -> None:
        self.assertIsNone(EvaluationAggregator().pass_at_k(()))


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
