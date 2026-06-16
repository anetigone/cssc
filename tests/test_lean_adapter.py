from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent.proof_system.lean import LeanAdapter
from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    DiagnosticCategory,
    ProofTask,
)
from agent.runtime.workspace import AttemptWorkspace


def has_usable_lean() -> bool:
    lean = shutil.which("lean")
    if lean is None:
        return False
    completed = subprocess.run(
        [lean, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.returncode == 0


class LeanAdapterTests(unittest.TestCase):
    def test_render_candidate_replaces_single_hole(self) -> None:
        adapter = LeanAdapter()
        task = ProofTask(
            task_id="true",
            source_template="theorem sample : True := by\n  {{proof}}\n",
        )
        source = adapter.render_candidate(task, CandidateEdit("trivial"))
        self.assertIn("trivial", source)
        self.assertNotIn("{{proof}}", source)

    def test_missing_tool_is_structured_result(self) -> None:
        adapter = LeanAdapter(lean_executable="definitely_missing_lean", lake_executable=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Attempt.lean"
            path.write_text("theorem sample : True := by trivial\n", encoding="utf-8")
            result = adapter.check(path, BudgetSlice(timeout_seconds=1))
        self.assertFalse(result.accepted)
        self.assertEqual(result.category, DiagnosticCategory.TOOL_UNAVAILABLE)

    def test_parse_sorry_warning_as_unsolved_goals(self) -> None:
        adapter = LeanAdapter()
        feedback = adapter.parse_feedback(
            "Attempt.lean:1:8: warning: declaration uses sorry\n"
        )
        self.assertEqual(feedback.category, DiagnosticCategory.UNSOLVED_GOALS)

    @unittest.skipIf(not has_usable_lean(), "Lean is not installed or no toolchain is configured")
    def test_accepts_valid_lean_file(self) -> None:
        adapter = LeanAdapter(prefer_lake=False)
        task = ProofTask(
            task_id="true",
            source_template="theorem sample : True := by\n  {{proof}}\n",
        )
        edit = CandidateEdit("trivial")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = AttemptWorkspace(tmp)
            source = adapter.render_candidate(task, edit)
            candidate = workspace.write_candidate(task, edit, source)
            result = adapter.check(candidate.path, BudgetSlice(timeout_seconds=10))
        self.assertTrue(result.accepted, result.raw_output)
        self.assertEqual(result.category, DiagnosticCategory.PROOF_ACCEPTED)

    @unittest.skipIf(not has_usable_lean(), "Lean is not installed or no toolchain is configured")
    def test_rejects_sorry_even_when_lean_exits_zero(self) -> None:
        adapter = LeanAdapter(prefer_lake=False, disallow_sorry=True)
        task = ProofTask(
            task_id="sorry",
            source_template="theorem sample : True := by\n  {{proof}}\n",
        )
        edit = CandidateEdit("sorry")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = AttemptWorkspace(tmp)
            source = adapter.render_candidate(task, edit)
            candidate = workspace.write_candidate(task, edit, source)
            result = adapter.check(candidate.path, BudgetSlice(timeout_seconds=10))
        self.assertFalse(result.accepted)
        self.assertEqual(result.category, DiagnosticCategory.UNSOLVED_GOALS)


if __name__ == "__main__":
    unittest.main()
