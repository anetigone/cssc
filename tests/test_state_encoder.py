from __future__ import annotations

import unittest

from agent.proof_system.base import DiagnosticCategory, ParsedFeedback, ProofTask
from agent.search.state_encoder import encode_proof_state


class StateEncoderTests(unittest.TestCase):
    def test_encodes_task_and_recent_feedback(self) -> None:
        task = ProofTask(
            "sample",
            "import Mathlib\n\ntheorem sample : True := by\n  {{proof}}\n",
            metadata={"proof_system": "lean4", "source_imports": ("Init",)},
        )
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            unsolved_goals=("⊢ True",),
        )

        state = encode_proof_state(task, feedback_history=(feedback,))

        self.assertEqual(state.task_id, "sample")
        self.assertEqual(state.imports, ("Init", "Mathlib"))
        self.assertEqual(state.declarations, ("sample",))
        self.assertEqual(state.recent_error_category, DiagnosticCategory.UNSOLVED_GOALS)
        self.assertEqual(state.goals, ("⊢ True",))
        self.assertIn("recent_error: unsolved_goals", state.to_prompt_context())


if __name__ == "__main__":
    unittest.main()
