from __future__ import annotations

import unittest

from agent.retrieval import LexicalLeanRetriever


class LexicalLeanRetrieverTests(unittest.TestCase):
    def test_retrieves_matching_declaration(self) -> None:
        source = """
theorem add_zero_demo (n : Nat) : n + 0 = n := by
  simp

lemma and_comm_demo (p q : Prop) : p ∧ q -> q ∧ p := by
  intro h
  exact And.intro h.right h.left
"""
        retriever = LexicalLeanRetriever.from_sources({"Demo.lean": source})

        results = retriever.retrieve("comm and right left", top_k=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "and_comm_demo")
        self.assertEqual(results[0].source_path, "Demo.lean")
        self.assertGreater(results[0].score, 0)


if __name__ == "__main__":
    unittest.main()
