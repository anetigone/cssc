from __future__ import annotations

import unittest

from agent.proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
    observation_from_dict,
    observations_from_check_result,
)


def _feedback(
    category: DiagnosticCategory = DiagnosticCategory.UNSOLVED_GOALS,
    *,
    goals: tuple[GoalState, ...] = (),
    message: str = "unsolved goals",
) -> ParsedFeedback:
    return ParsedFeedback(
        category=category,
        message=message,
        goal_state=goals,
    )


class ObservationSerializationTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        obs = Observation(
            observation_id="attempt:2:goal:0",
            source=ObservationSource.CHECKER,
            category=DiagnosticCategory.UNSOLVED_GOALS.value,
            message="n : Nat |- n + 0 = n",
            declaration_id="helper",
            goal_fingerprint="abc123",
            raw_evidence_ref="attempt:2",
        )
        restored = observation_from_dict(obs.to_dict())
        self.assertEqual(restored, obs)

    def test_default_source_is_checker(self) -> None:
        obs = observation_from_dict(
            {"observation_id": "o1", "category": "unknown"}
        )
        self.assertEqual(obs.source, ObservationSource.CHECKER)
        self.assertEqual(obs.raw_evidence_ref, "")


class ObservationsFromCheckResultTest(unittest.TestCase):
    def test_accepted_result_yields_no_observations(self) -> None:
        result = CheckResult(
            accepted=True,
            category=DiagnosticCategory.PROOF_ACCEPTED,
            raw_output="",
            parsed_feedback=_feedback(DiagnosticCategory.PROOF_ACCEPTED),
        )
        self.assertEqual(observations_from_check_result(result, 0), ())

    def test_one_observation_per_goal(self) -> None:
        result = CheckResult(
            accepted=False,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="unsolved goals",
            parsed_feedback=_feedback(
                goals=(
                    GoalState(
                        text="n : Nat\n|- n + 0 = n",
                        goal_fingerprint="abc123",
                        declaration_id="t1",
                    ),
                    GoalState(text="|- True", goal_fingerprint="def456"),
                )
            ),
        )
        observations = observations_from_check_result(result, 3)
        self.assertEqual(len(observations), 2)
        self.assertTrue(
            all(o.raw_evidence_ref == "attempt:3" for o in observations)
        )
        self.assertEqual(observations[0].goal_fingerprint, "abc123")
        self.assertEqual(observations[0].declaration_id, "t1")
        self.assertEqual(observations[0].observation_id, "attempt:3:goal:0")
        self.assertEqual(observations[1].observation_id, "attempt:3:goal:1")

    def test_no_goals_falls_back_to_summary(self) -> None:
        result = CheckResult(
            accepted=False,
            category=DiagnosticCategory.TYPE_MISMATCH,
            raw_output="type mismatch term",
            parsed_feedback=_feedback(
                DiagnosticCategory.TYPE_MISMATCH, message="type mismatch term"
            ),
        )
        observations = observations_from_check_result(result, 1)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].category, "type_mismatch")
        self.assertEqual(observations[0].observation_id, "attempt:1:summary")

    def test_missing_feedback_still_records_summary(self) -> None:
        result = CheckResult(
            accepted=False,
            category=DiagnosticCategory.CHECKER_ERROR,
            raw_output="boom",
        )
        observations = observations_from_check_result(result, 0)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].category, "checker_error")


if __name__ == "__main__":
    unittest.main()
