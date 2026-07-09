from __future__ import annotations

import unittest

from agent.proof_system.base import CheckResult, DiagnosticCategory
from agent.search.safety import SafetyVerdict, StatementSafetyReviewer
from agent.tasks.types import ProofTask


_TEMPLATE = "theorem sample : True := by\n  {{proof}}\n"


def _task() -> ProofTask:
    return ProofTask("sample", _TEMPLATE)


def _accepted_result() -> CheckResult:
    return CheckResult(
        accepted=True,
        category=DiagnosticCategory.PROOF_ACCEPTED,
        raw_output="",
    )


def _render(proof: str) -> str:
    return _TEMPLATE.replace("{{proof}}", proof)


class StatementSafetyReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reviewer = StatementSafetyReviewer()

    def test_accepts_clean_proof(self) -> None:
        verdict = self.reviewer.accepts(_task(), _render("trivial"), _accepted_result())

        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.reasons, ())

    def test_accepts_clean_proof_with_leading_doc_comment(self) -> None:
        template = (
            "/-- A documented theorem. -/\n"
            "theorem sample : True := by\n"
            "  {{proof}}\n"
        )
        task = ProofTask("sample", template)
        candidate = template.replace("{{proof}}", "trivial")

        verdict = self.reviewer.accepts(task, candidate, _accepted_result())

        self.assertTrue(verdict.accepted)

    def test_rejects_sorry(self) -> None:
        verdict = self.reviewer.accepts(_task(), _render("sorry"), _accepted_result())

        self.assertFalse(verdict.accepted)
        self.assertIn("residual_shortcut:sorry", verdict.reasons)

    def test_rejects_admit(self) -> None:
        verdict = self.reviewer.accepts(_task(), _render("admit"), _accepted_result())

        self.assertFalse(verdict.accepted)
        self.assertIn("residual_shortcut:admit", verdict.reasons)

    def test_rejects_new_axiom(self) -> None:
        candidate = "axiom magic : True\n" + _render("exact magic")
        verdict = self.reviewer.accepts(_task(), candidate, _accepted_result())

        self.assertFalse(verdict.accepted)
        self.assertIn("new_axiom_declared", verdict.reasons)

    def test_allows_axiom_present_in_template(self) -> None:
        template = "axiom base : True\ntheorem sample : True := by\n  {{proof}}\n"
        task = ProofTask("sample", template)
        candidate = template.replace("{{proof}}", "exact base")

        verdict = self.reviewer.accepts(task, candidate, _accepted_result())

        self.assertTrue(verdict.accepted)

    def test_existing_axiom_does_not_mask_new_axiom(self) -> None:
        template = "axiom base : True\ntheorem sample : True := by\n  {{proof}}\n"
        task = ProofTask("sample", template)
        candidate = template.replace("{{proof}}", "exact base") + "\naxiom injected : False\n"

        verdict = self.reviewer.accepts(task, candidate, _accepted_result())

        self.assertFalse(verdict.accepted)
        self.assertIn("new_axiom_declared", verdict.reasons)

    def test_rejects_rewritten_statement_header(self) -> None:
        # The model changed the theorem signature before the hole.
        candidate = (
            "theorem renamed : False := by\n  trivial\n"
        )
        verdict = self.reviewer.accepts(_task(), candidate, _accepted_result())

        self.assertFalse(verdict.accepted)
        self.assertIn("statement_not_preserved", verdict.reasons)

    def test_rejects_statement_header_only_preserved_in_comment(self) -> None:
        candidate = (
            "-- theorem sample : True := by\n"
            "theorem renamed : False := by\n"
            "  trivial\n"
        )
        verdict = self.reviewer.accepts(_task(), candidate, _accepted_result())

        self.assertFalse(verdict.accepted)
        self.assertIn("statement_not_preserved", verdict.reasons)

    def test_allows_prepended_helper_declarations(self) -> None:
        # Phase 7.4: a structured assembly prepends helper declarations before
        # the root. The root statement is still present verbatim, so the
        # statement-preservation check must accept it (shortcut/axiom scans
        # still run over the whole candidate to catch cheating in helpers).
        candidate = (
            "lemma helper : True := by trivial\n"
            "\n"
            "theorem sample : True := by\n"
            "  exact helper\n"
        )
        verdict = self.reviewer.accepts(_task(), candidate, _accepted_result())

        self.assertTrue(verdict.accepted)

    def test_allows_helper_declarations_after_template_preamble(self) -> None:
        template = (
            "import Mathlib.Data.Nat.Basic\n"
            "\n"
            "theorem sample : True := by\n"
            "  {{proof}}\n"
        )
        task = ProofTask("sample", template)
        candidate = (
            "import Mathlib.Data.Nat.Basic\n"
            "lemma helper : True := by trivial\n"
            "\n"
            "theorem sample : True := by\n"
            "  exact helper\n"
        )
        verdict = self.reviewer.accepts(task, candidate, _accepted_result())

        self.assertTrue(verdict.accepted)

    def test_ignores_shortcut_words_in_comments_and_strings(self) -> None:
        candidate = _render(
            'have label : String := "sorry axiom"\n'
            "  /- nested /- admit -/ axiom -/\n"
            "  trivial -- do not use sorry"
        )

        verdict = self.reviewer.accepts(_task(), candidate, _accepted_result())

        self.assertTrue(verdict.accepted)


if __name__ == "__main__":
    unittest.main()
