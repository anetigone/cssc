from __future__ import annotations

import unittest

from agent.proof_system.base import DiagnosticCategory, ParsedFeedback, ProofTask
from agent.search.action import ActionGenerationRequest
from agent.search.repair import FeedbackRepairGenerator


class FeedbackRepairGeneratorTests(unittest.TestCase):
    def test_generates_repairs_for_unknown_identifier(self) -> None:
        generator = FeedbackRepairGenerator()
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
            message="unknown identifier 'foo'",
        )
        request = ActionGenerationRequest(
            task=ProofTask("t", "theorem t : True := by\n  {{proof}}\n"),
            attempt_index=1,
            previous_feedback=(feedback,),
            max_candidates=2,
        )

        actions = generator.generate(request)

        self.assertEqual(len(actions), 2)
        self.assertTrue(all(action.action == "repair" for action in actions))
        self.assertEqual(actions[0].metadata["repair_reason"], "unknown_identifier_remove_reference")

    def test_respects_max_candidates_without_feedback(self) -> None:
        request = ActionGenerationRequest(
            task=ProofTask("t", "theorem t : True := by\n  {{proof}}\n"),
            attempt_index=0,
            max_candidates=1,
        )

        actions = FeedbackRepairGenerator().generate(request)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].proof_text, "simp")


if __name__ == "__main__":
    unittest.main()
