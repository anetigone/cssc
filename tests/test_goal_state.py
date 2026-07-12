from __future__ import annotations

import unittest

from agent.proof_system.lean_feedback import (
    LeanFeedbackParser,
    extract_goal_states,
)
from agent.search.metrics import goal_fingerprint


_SORRY_GOAL_OUTPUT = """file://./Demo.lean:7:6: error: unsolved goals
⊢ C l λ R = 0

sorry was used here
"""

_MULTI_GOAL_OUTPUT = """Demo.lean:3:2: error: unsolved goals
⊢ n + 0 = n

Demo.lean:9:2: error: unsolved goals
⊢ m + 0 = m
"""


class ExtractGoalStatesTests(unittest.TestCase):
    def test_flags_sorry_goal_and_fingerprints_text(self) -> None:
        states = extract_goal_states(_SORRY_GOAL_OUTPUT, source_span=(7, 6))

        self.assertEqual(len(states), 1)
        goal = states[0]
        self.assertIn("C l", goal.text)
        self.assertTrue(goal.is_sorry_goal)
        self.assertEqual(goal.goal_fingerprint, goal_fingerprint(goal.text))
        self.assertEqual(goal.source_span, (7, 6))

    def test_non_sorry_goal_is_not_flagged(self) -> None:
        states = extract_goal_states(_MULTI_GOAL_OUTPUT)

        self.assertEqual(len(states), 2)
        self.assertFalse(states[0].is_sorry_goal)
        self.assertFalse(states[1].is_sorry_goal)
        self.assertNotEqual(states[0].goal_fingerprint, states[1].goal_fingerprint)

    def test_empty_output_has_no_goal_state(self) -> None:
        self.assertEqual(extract_goal_states(""), ())


class ParserGoalStateTests(unittest.TestCase):
    def test_parse_populates_both_legacy_and_structured_goals(self) -> None:
        parser = LeanFeedbackParser()
        feedback = parser.parse(_MULTI_GOAL_OUTPUT)

        self.assertEqual(len(feedback.unsolved_goals), 2)
        self.assertEqual(len(feedback.goal_state), 2)
        self.assertEqual(feedback.line, 3)
        self.assertEqual(feedback.column, 2)
        self.assertEqual(feedback.goal_state[0].source_span, (3, 2))
        # Identity agrees with the baseline fingerprint helper.
        self.assertEqual(
            feedback.goal_state[0].goal_fingerprint,
            goal_fingerprint(feedback.unsolved_goals[0]),
        )

    def test_parse_accepts_without_goal_state(self) -> None:
        parser = LeanFeedbackParser()
        feedback = parser.parse("")

        self.assertEqual(feedback.goal_state, ())
        self.assertEqual(feedback.unsolved_goals, ())


if __name__ == "__main__":
    unittest.main()
