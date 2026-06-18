from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.input.validation import (
    LeanAdapterScaffoldChecker,
    ScaffoldValidationError,
    ScaffoldValidationResult,
    ValidationConfig,
    validate_scaffold_json,
)
from agent.proof_system.base import BudgetSlice, CheckResult, DiagnosticCategory
from agent.proof_system.lean import LeanAdapter


class ValidateScaffoldJsonTests(unittest.TestCase):
    def test_accepts_valid_scaffold(self) -> None:
        proof_source, nl_proof = validate_scaffold_json({
            "proof_source": "theorem sample : True := by\n  sorry\n",
            "natural_language_proof": "True is trivial.",
        })
        self.assertEqual(proof_source, "theorem sample : True := by\n  sorry")
        self.assertEqual(nl_proof, "True is trivial.")

    def test_proof_source_is_required(self) -> None:
        with self.assertRaises(ScaffoldValidationError) as ctx:
            validate_scaffold_json({})
        self.assertEqual(ctx.exception.stage, "json_shape")

    def test_proof_source_must_be_non_empty(self) -> None:
        with self.assertRaises(ScaffoldValidationError) as ctx:
            validate_scaffold_json({"proof_source": "   "})
        self.assertEqual(ctx.exception.stage, "json_shape")

    def test_accepts_aliases(self) -> None:
        for key in ("lean", "source_template"):
            with self.subTest(key=key):
                proof_source, _ = validate_scaffold_json({key: "theorem sample : True := by sorry"})
                self.assertEqual(proof_source, "theorem sample : True := by sorry")

    def test_accepts_informal_proof_alias(self) -> None:
        _, nl_proof = validate_scaffold_json({
            "proof_source": "theorem sample : True := by sorry",
            "informal_proof": "Use trivial.",
        })
        self.assertEqual(nl_proof, "Use trivial.")

    def test_rejects_non_string_proof_source(self) -> None:
        with self.assertRaises(ScaffoldValidationError) as ctx:
            validate_scaffold_json({"proof_source": 123})
        self.assertEqual(ctx.exception.stage, "json_shape")

    def test_rejects_non_string_proof(self) -> None:
        with self.assertRaises(ScaffoldValidationError) as ctx:
            validate_scaffold_json({
                "proof_source": "theorem sample : True := by sorry",
                "natural_language_proof": 123,
            })
        self.assertEqual(ctx.exception.stage, "json_shape")


class FakeLeanAdapter:
    """Stubbed adapter for scaffold checker tests."""

    def __init__(self, category: DiagnosticCategory, accepted: bool = False) -> None:
        self.category = category
        self.accepted = accepted
        self.calls: list[tuple[Path, BudgetSlice]] = []
        self.sources: list[str] = []

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        self.calls.append((candidate_file, budget_slice))
        self.sources.append(candidate_file.read_text(encoding="utf-8"))
        return CheckResult(
            accepted=self.accepted,
            category=self.category,
            raw_output=f"category={self.category.value}",
            candidate_file=candidate_file,
        )


class LeanAdapterScaffoldCheckerTests(unittest.TestCase):
    def test_fills_marker_with_sorry(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        checker = LeanAdapterScaffoldChecker(adapter)

        result = checker.validate_scaffold(
            "theorem sample : True := by\n  {{proof}}\n",
            hole_marker="{{proof}}",
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(adapter.calls), 1)
        written = adapter.sources[0]
        self.assertIn("sorry", written)
        self.assertNotIn("{{proof}}", written)

    def test_passes_imports_to_source(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        checker = LeanAdapterScaffoldChecker(adapter)

        checker.validate_scaffold(
            "theorem sample : True := by\n  sorry\n",
            imports=("Mathlib.Data.Nat.Basic",),
        )

        written = adapter.sources[0]
        self.assertIn("import Mathlib.Data.Nat.Basic", written)

    def test_returns_failure_when_lean_rejects(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PARSER_ERROR, accepted=False)
        checker = LeanAdapterScaffoldChecker(adapter)

        result = checker.validate_scaffold("theorem sample : True := by\n  sorry\n")

        self.assertFalse(result.ok)
        self.assertEqual(result.category, DiagnosticCategory.PARSER_ERROR)
        self.assertIn("category=parser_error", result.message)

    def test_rejects_bare_mathlib_import_without_running_lean(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        checker = LeanAdapterScaffoldChecker(adapter)

        result = checker.validate_scaffold(
            "import Mathlib\n\ntheorem sample : True := by\n  sorry\n"
        )

        self.assertFalse(result.ok)
        self.assertIn("Bare `import Mathlib` is not allowed", result.message)
        self.assertEqual(adapter.calls, [])

    def test_allows_narrow_mathlib_import(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        checker = LeanAdapterScaffoldChecker(adapter)

        result = checker.validate_scaffold(
            "import Mathlib.Data.Nat.Basic\n\ntheorem sample : True := by\n  sorry\n"
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(adapter.calls), 1)

    def test_skips_validation_when_tool_unavailable(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.TOOL_UNAVAILABLE, accepted=False)
        checker = LeanAdapterScaffoldChecker(adapter)

        result = checker.validate_scaffold("theorem sample : True := by\n  sorry\n")

        self.assertTrue(result.ok)
        self.assertEqual(result.category, DiagnosticCategory.TOOL_UNAVAILABLE)

    def test_creates_temporary_workspace_when_none_provided(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        checker = LeanAdapterScaffoldChecker(adapter)

        checker.validate_scaffold("theorem sample : True := by\n  sorry\n")

        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("sorry", adapter.sources[0])

    def test_uses_provided_workspace(self) -> None:
        from agent.runtime.workspace import EphemeralCheckWorkspace

        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = EphemeralCheckWorkspace(tmp, keep_files=True)
            checker = LeanAdapterScaffoldChecker(adapter, workspace=workspace)

            checker.validate_scaffold("theorem sample : True := by\n  sorry\n")

            self.assertIn("scaffold_check", str(adapter.calls[0][0]))
            self.assertTrue(adapter.calls[0][0].exists())

    def test_respects_validation_config_timeout(self) -> None:
        adapter = FakeLeanAdapter(DiagnosticCategory.PROOF_ACCEPTED, accepted=True)
        checker = LeanAdapterScaffoldChecker(
            adapter,
            validation=ValidationConfig(check_timeout_seconds=5.0),
        )

        checker.validate_scaffold("theorem sample : True := by\n  sorry\n")

        self.assertEqual(adapter.calls[0][1].timeout_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
