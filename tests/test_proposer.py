from __future__ import annotations

import unittest

from agent.proof_system.base import ProofTask
from agent.search.action import ActionGenerationRequest
from agent.search.proposer import CandidateLibraryGenerator, ProofSnippet


class CandidateLibraryGeneratorTests(unittest.TestCase):
    def test_returns_library_snippets_as_candidates(self) -> None:
        generator = CandidateLibraryGenerator(
            [
                ProofSnippet("simp", score=0.4, metadata={"source": "default"}),
                "trivial",
            ]
        )
        request = ActionGenerationRequest(
            task=ProofTask("t", "theorem t : True := by\n  {{proof}}\n"),
            attempt_index=0,
            max_candidates=2,
        )

        actions = generator.generate(request)

        self.assertEqual([action.proof_text for action in actions], ["simp", "trivial"])
        self.assertEqual(actions[0].action, "library")
        self.assertEqual(actions[0].metadata["source"], "default")


if __name__ == "__main__":
    unittest.main()
