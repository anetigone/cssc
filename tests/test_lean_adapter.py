from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.proof_system.lean import LeanAdapter
from agent.proof_system.lean_server import LeanServerAmbiguousCompletion
from agent.proof_system.lean_subprocess import ProcessGroupRunner, kill_process_tree
from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
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
    def test_ambiguous_server_completion_falls_back_from_server(self) -> None:
        adapter = LeanAdapter(use_server=True)
        server = MagicMock()
        server.is_alive.return_value = True
        server.command = ("lean", "--server")
        server.check_file.side_effect = LeanServerAmbiguousCompletion("ambiguous")
        adapter._server = server

        result = adapter._check_with_server(
            Path("Attempt.lean"), BudgetSlice(timeout_seconds=1)
        )

        self.assertIsNone(result)
        server.close.assert_called_once()
        self.assertIsNone(adapter._server)

    def test_subprocess_timeout_kills_before_draining_output(self) -> None:
        process = MagicMock()
        process.returncode = -9
        killed = False

        def communicate(*, timeout: float | None = None):
            nonlocal killed
            if not killed:
                raise subprocess.TimeoutExpired(
                    cmd=["lean"], timeout=0.1, output="partial", stderr="warning"
                )
            return "drained", ""

        def kill_tree(_process: object) -> None:
            nonlocal killed
            killed = True

        process.communicate.side_effect = communicate
        with (
            patch("agent.proof_system.lean_subprocess.subprocess.Popen", return_value=process),
            patch("agent.proof_system.lean_subprocess.kill_process_tree", side_effect=kill_tree) as kill,
        ):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                ProcessGroupRunner().run(
                    ["lean", "Attempt.lean"],
                    cwd=Path("."),
                    timeout_seconds=0.1,
                )

        kill.assert_called_once_with(process)
        self.assertEqual(caught.exception.output, "drained")

    def test_windows_process_tree_uses_taskkill_tree_force(self) -> None:
        process = MagicMock()
        process.pid = 1234
        process.poll.return_value = None
        with (
            patch("agent.proof_system.lean_subprocess.sys.platform", "win32"),
            patch("agent.proof_system.lean_subprocess.subprocess.run") as run,
        ):
            kill_process_tree(process)

        command = run.call_args.args[0]
        self.assertEqual(command, ["taskkill", "/PID", "1234", "/T", "/F"])
        process.kill.assert_called_once()

    def test_restarts_server_and_retries_unchanged_candidate_after_timeout(self) -> None:
        adapter = LeanAdapter(
            use_server=True,
            lean_executable="lean",
            server_timeout_retries=1,
        )
        timeout_feedback = ParsedFeedback(
            category=DiagnosticCategory.TIMEOUT,
            message="timed out",
            raw_output="timed out",
        )
        accepted_feedback = ParsedFeedback(
            category=DiagnosticCategory.PROOF_ACCEPTED,
            message="accepted",
        )
        timeout_result = CheckResult(
            accepted=False,
            category=DiagnosticCategory.TIMEOUT,
            raw_output="timed out",
            parsed_feedback=timeout_feedback,
        )
        accepted_result = CheckResult(
            accepted=True,
            category=DiagnosticCategory.PROOF_ACCEPTED,
            raw_output="",
            parsed_feedback=accepted_feedback,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Attempt.lean"
            path.write_text("theorem sample : True := by trivial\n", encoding="utf-8")
            with patch.object(adapter, "_build_command", return_value=["lean", str(path)]):
                with patch.object(
                    adapter,
                    "_check_with_server",
                    side_effect=[timeout_result, accepted_result],
                ) as check_server:
                    with patch.object(adapter, "close") as close:
                        result = adapter.check(path, BudgetSlice(timeout_seconds=1))

        self.assertTrue(result.accepted)
        self.assertEqual(check_server.call_count, 2)
        close.assert_called_once()

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

    def test_parse_feedback_prioritizes_error_over_information_and_warning(self) -> None:
        adapter = LeanAdapter()
        feedback = adapter.parse_feedback(
            "Attempt.lean:1:1: information: theorem type\n"
            "Attempt.lean:2:1: warning: try simp\n"
            "Attempt.lean:9:4: error: Type mismatch: bad term\n"
        )
        self.assertEqual(feedback.category, DiagnosticCategory.TYPE_MISMATCH)
        self.assertEqual(feedback.line, 9)
        self.assertEqual(feedback.column, 4)
        self.assertIn("Type mismatch", feedback.message)

    def test_subprocess_output_uses_utf8_replacement_decoding(self) -> None:
        adapter = LeanAdapter(
            prefer_lake=False,
            lean_executable="lean",
            lake_executable=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Attempt.lean"
            path.write_text("theorem sample : True := by\n  exact False.elim\n", encoding="utf-8")
            with patch.object(adapter._runner, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=("lean", str(path)),
                    returncode=1,
                    stdout="",
                    stderr="Attempt.lean:2:8: error: application type mismatch\n",
                )

                result = adapter.check(path, BudgetSlice(timeout_seconds=1))

        self.assertFalse(result.accepted)
        self.assertEqual(result.category, DiagnosticCategory.TYPE_MISMATCH)
        # The runner is invoked with utf-8 / replace decoding so non-UTF-8
        # Lean output never raises during capture.
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_nonzero_exit_without_output_is_checker_error(self) -> None:
        adapter = LeanAdapter(
            prefer_lake=False,
            lean_executable="lean",
            lake_executable=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Attempt.lean"
            path.write_text("theorem sample : True := by\n  exact False.elim\n", encoding="utf-8")
            with patch.object(adapter._runner, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=("lean", str(path)),
                    returncode=1,
                    stdout="",
                    stderr="",
                )

                result = adapter.check(path, BudgetSlice(timeout_seconds=1))

        self.assertFalse(result.accepted)
        self.assertEqual(result.category, DiagnosticCategory.CHECKER_ERROR)
        assert result.parsed_feedback is not None
        self.assertIn("without diagnostic output", result.parsed_feedback.message)

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

    def test_allows_sorry_warning_when_configured_for_scaffold_validation(self) -> None:
        adapter = LeanAdapter(
            prefer_lake=False,
            disallow_sorry=False,
            lean_executable="lean",
            lake_executable=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Attempt.lean"
            path.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")
            with patch.object(adapter._runner, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=("lean", str(path)),
                    returncode=0,
                    stdout="Attempt.lean:1:9: warning: declaration uses `sorry`\n",
                    stderr="",
                )

                result = adapter.check(path, BudgetSlice(timeout_seconds=1))

        self.assertTrue(result.accepted)
        self.assertEqual(result.category, DiagnosticCategory.PROOF_ACCEPTED)


if __name__ == "__main__":
    unittest.main()
