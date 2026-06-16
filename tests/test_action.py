from __future__ import annotations

import unittest

from agent.search.action import ActionCandidate, ActionGenerationRequest, StaticActionGenerator
from agent.proof_system.base import ProofTask


class ActionTests(unittest.TestCase):
    def test_action_candidate_converts_to_candidate_edit(self) -> None:
        candidate = ActionCandidate(
            proof_text="trivial",
            action="model_complete",
            score=0.75,
            metadata={"temperature": 0.2},
        )

        edit = candidate.to_edit(parent_node_id="root")

        self.assertEqual(edit.text, "trivial")
        self.assertEqual(edit.action, "model_complete")
        self.assertEqual(edit.parent_node_id, "root")
        self.assertEqual(edit.metadata["score"], 0.75)
        self.assertEqual(edit.metadata["temperature"], 0.2)

    def test_static_action_generator_respects_max_candidates(self) -> None:
        generator = StaticActionGenerator(["first", "second"])
        request = ActionGenerationRequest(
            task=ProofTask("t", "{{proof}}"),
            attempt_index=0,
            max_candidates=1,
        )

        actions = generator.generate(request)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].proof_text, "first")
        self.assertEqual(actions[0].action, "static")


if __name__ == "__main__":
    unittest.main()
