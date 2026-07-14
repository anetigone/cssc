"""Lean 4 implementation of the proof-system adapter."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from .lean_command import LeanCommandBuilder
from .lean_feedback import LeanFeedbackParser, contains_error_diagnostic, contains_sorry_warning
from .lean_project import LakeProject
from .lean_server import LeanServerClient, LeanServerError, LeanServerTimeout
from .lean_subprocess import ProcessGroupRunner, combine_output


logger = logging.getLogger(__name__)


class LeanAdapter(ProofSystemAdapter):
    """Thin Lean checker adapter.

    The adapter prefers `lake env lean` when it is inside a Lake project and
    falls back to `lean` for standalone files. Missing tools are reported as
    structured checker results so tests and controllers can handle them cleanly.
    """

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        prefer_lake: bool = True,
        disallow_sorry: bool = True,
        lean_executable: str | None = None,
        lake_executable: str | None = None,
        use_server: bool = False,
        server_startup_timeout_seconds: float = 60.0,
        server_timeout_retries: int = 1,
        server_fallback_seconds: float = 2.0,
        require_server: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve() if project_root else None
        self.prefer_lake = prefer_lake
        self.disallow_sorry = disallow_sorry
        self.use_server = use_server
        self.server_startup_timeout_seconds = server_startup_timeout_seconds
        self.server_timeout_retries = server_timeout_retries
        self.server_fallback_seconds = server_fallback_seconds
        self.require_server = require_server
        if require_server and not use_server:
            raise ValueError("require_server=True requires use_server=True")
        self._project = LakeProject(self.project_root)
        self._command_builder = LeanCommandBuilder(
            self._project,
            prefer_lake=prefer_lake,
            lean_executable=lean_executable,
            lake_executable=lake_executable,
        )
        self._feedback_parser = LeanFeedbackParser()
        self._runner = ProcessGroupRunner()
        self._server: LeanServerClient | None = None
        logger.debug(
            "Initialized LeanAdapter: project_root=%s prefer_lake=%s disallow_sorry=%s lean=%s lake=%s use_server=%s",
            self.project_root,
            self.prefer_lake,
            self.disallow_sorry,
            self._command_builder.lean_executable,
            self._command_builder.lake_executable,
            self.use_server,
        )

    def render_candidate(
        self,
        task: ProofTask,
        candidate_edit: CandidateEdit,
        *,
        holes: tuple[int, ...] | None = None,
    ) -> str:
        """Render a complete source file from a task template and candidate.

        Multi-hole seam: ``holes`` is reserved for future controllers that want
        to render a specific subset of holes. ``None`` preserves the current
        single-active-marker behavior.
        """
        # TODO: multi-hole — honor ``holes`` when rendering subsets of holes.
        if task.hole_marker not in task.source_template:
            logger.error(
                "Task template is missing marker: task_id=%s marker=%s",
                task.task_id,
                task.hole_marker,
            )
            raise ValueError(
                f"Task {task.task_id!r} template is missing marker {task.hole_marker!r}"
            )

        rendered = task.source_template.replace(task.hole_marker, candidate_edit.text)
        if task.imports:
            import_block = "\n".join(f"import {module}" for module in task.imports)
            rendered = f"{import_block}\n\n{rendered}"
        return rendered

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        candidate_file = Path(candidate_file).resolve()
        command = self._build_command(candidate_file)
        if command is None:
            raw = "Lean checker unavailable: neither lake nor lean was found on PATH."
            logger.warning("Lean checker unavailable for candidate_file=%s", candidate_file)
            feedback = ParsedFeedback(
                category=DiagnosticCategory.TOOL_UNAVAILABLE,
                message=raw,
                raw_output=raw,
            )
            return CheckResult(
                accepted=False,
                category=DiagnosticCategory.TOOL_UNAVAILABLE,
                raw_output=raw,
                candidate_file=candidate_file,
                parsed_feedback=feedback,
                progress=self.extract_progress(None, _minimal_result(feedback)),
            )

        for server_attempt in range(self.server_timeout_retries + 1):
            server_result = self._check_with_server(candidate_file, budget_slice)
            if server_result is None:
                break
            if (
                server_result.category != DiagnosticCategory.TIMEOUT
                or server_attempt >= self.server_timeout_retries
            ):
                return server_result
            logger.warning(
                "Lean server check timed out; restarting and retrying unchanged candidate "
                "(%d/%d): candidate_file=%s",
                server_attempt + 1,
                self.server_timeout_retries,
                candidate_file,
            )
            self.close()

        if self.require_server:
            return self._server_failure_result(
                candidate_file,
                "Persistent Lean server is required but could not complete the check.",
            )

        started = time.perf_counter()
        logger.debug(
            "Running Lean checker: command=%s cwd=%s timeout=%s",
            command,
            self.project_root or candidate_file.parent,
            budget_slice.timeout_seconds,
        )
        try:
            completed = self._runner.run(
                command,
                cwd=self.project_root or candidate_file.parent,
                timeout_seconds=budget_slice.timeout_seconds,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            logger.warning(
                "Lean checker timed out: candidate_file=%s timeout=%s elapsed=%.3f",
                candidate_file,
                budget_slice.timeout_seconds,
                elapsed,
            )
            raw = combine_output(exc.stdout, exc.stderr)
            if raw:
                raw = f"{raw}\nLean checker timed out after {budget_slice.timeout_seconds}s."
            else:
                raw = f"Lean checker timed out after {budget_slice.timeout_seconds}s."
            feedback = ParsedFeedback(
                category=DiagnosticCategory.TIMEOUT,
                message=raw,
                raw_output=raw,
            )
            result = CheckResult(
                accepted=False,
                category=DiagnosticCategory.TIMEOUT,
                raw_output=raw,
                candidate_file=candidate_file,
                command=tuple(command),
                exit_code=None,
                elapsed_seconds=elapsed,
                parsed_feedback=feedback,
            )
            return _with_progress(self, result)
        except OSError as exc:
            elapsed = time.perf_counter() - started
            logger.warning(
                "Lean checker failed to run: candidate_file=%s error=%s",
                candidate_file,
                exc,
            )
            raw = f"Lean checker failed to run: {exc}"
            feedback = ParsedFeedback(
                category=DiagnosticCategory.CHECKER_ERROR,
                message=raw,
                raw_output=raw,
            )
            result = CheckResult(
                accepted=False,
                category=DiagnosticCategory.CHECKER_ERROR,
                raw_output=raw,
                candidate_file=candidate_file,
                command=tuple(command),
                exit_code=None,
                elapsed_seconds=elapsed,
                parsed_feedback=feedback,
            )
            return _with_progress(self, result)

        elapsed = time.perf_counter() - started
        raw = combine_output(completed.stdout, completed.stderr)
        feedback = self.parse_feedback(raw)
        if completed.returncode != 0 and feedback.category == DiagnosticCategory.PROOF_ACCEPTED:
            feedback = ParsedFeedback(
                category=DiagnosticCategory.CHECKER_ERROR,
                message=f"Lean exited with code {completed.returncode} without diagnostic output.",
                raw_output=raw,
            )
        logger.debug(
            "Lean checker completed: candidate_file=%s exit_code=%s category=%s elapsed=%.3f",
            candidate_file,
            completed.returncode,
            feedback.category.value,
            elapsed,
        )

        feedback = self._finalize_feedback(feedback, raw, completed.returncode == 0)
        accepted = completed.returncode == 0 and feedback.category == DiagnosticCategory.PROOF_ACCEPTED

        result = CheckResult(
            accepted=accepted,
            category=feedback.category if not accepted else DiagnosticCategory.PROOF_ACCEPTED,
            raw_output=raw,
            candidate_file=candidate_file,
            command=tuple(command),
            exit_code=completed.returncode,
            elapsed_seconds=elapsed,
            parsed_feedback=feedback,
        )
        return _with_progress(self, result)

    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        return self._feedback_parser.parse(raw_output)

    def extract_progress(
        self,
        parent_state: Any,
        check_result: CheckResult,
    ) -> ProgressSignal:
        feedback = check_result.parsed_feedback
        category = feedback.category if feedback else check_result.category
        semantic = category in {
            DiagnosticCategory.UNSOLVED_GOALS,
            DiagnosticCategory.TACTIC_FAILED,
            DiagnosticCategory.TYPE_MISMATCH,
        }
        return ProgressSignal(
            diagnostic_category=category,
            moved_to_semantic_obligation=semantic,
            features={
                "accepted": check_result.accepted,
                "exit_code": check_result.exit_code,
                "elapsed_seconds": check_result.elapsed_seconds,
            },
        )

    def _build_command(self, candidate_file: Path) -> list[str] | None:
        return self._command_builder.build_check_command(candidate_file)

    def _build_server_command(self) -> list[str] | None:
        return self._command_builder.build_server_command()

    def _has_lake_project(self) -> bool:
        return self._project.is_lake_project

    def start_service(self, *, timeout_seconds: float = 10.0) -> bool:
        """Start a persistent Lean language server for repeated checks."""

        if not self.use_server:
            return False
        if self._server is not None and self._server.is_alive():
            return True
        if self._server is not None:
            logger.warning("Discarding unhealthy Lean server before restart")
            self.close()
        command = self._build_server_command()
        if command is None:
            return False
        server: LeanServerClient | None = None
        try:
            server = LeanServerClient(
                command,
                cwd=self.project_root,
                root=self.project_root,
                diagnostics_fallback_seconds=self.server_fallback_seconds,
            )
            server.start(timeout_seconds=timeout_seconds)
        except (OSError, LeanServerError):
            logger.warning("Failed to start Lean server; falling back to subprocess checks", exc_info=True)
            if server is not None:
                server.close()
            self._server = None
            return False

        self._server = server
        logger.info("Started Lean server: command=%s cwd=%s", command, self.project_root)
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def subprocess_clone(self) -> "LeanAdapter":
        """Return an equivalent adapter that uses the subprocess checker."""

        return LeanAdapter(
            project_root=self.project_root,
            prefer_lake=self.prefer_lake,
            disallow_sorry=self.disallow_sorry,
            lean_executable=self._command_builder.lean_executable,
            lake_executable=self._command_builder.lake_executable,
            use_server=False,
            server_startup_timeout_seconds=self.server_startup_timeout_seconds,
            server_timeout_retries=self.server_timeout_retries,
            server_fallback_seconds=self.server_fallback_seconds,
            require_server=False,
        )

    def _check_with_server(
        self,
        candidate_file: Path,
        budget_slice: BudgetSlice,
    ) -> CheckResult | None:
        if not self.use_server:
            return None
        if self._server is None or not self._server.is_alive():
            self.start_service(timeout_seconds=self.server_startup_timeout_seconds)
        if self._server is None:
            return None

        command = self._server.command
        started = time.perf_counter()
        logger.info(
            "Lean server check started: candidate_file=%s timeout=%s",
            candidate_file,
            budget_slice.timeout_seconds,
        )
        try:
            server_result = self._server.check_file(candidate_file, timeout_seconds=budget_slice.timeout_seconds)
        except LeanServerTimeout as exc:
            elapsed = time.perf_counter() - started
            raw = str(exc)
            feedback = ParsedFeedback(
                category=DiagnosticCategory.TIMEOUT,
                message=raw,
                raw_output=raw,
            )
            result = CheckResult(
                accepted=False,
                category=DiagnosticCategory.TIMEOUT,
                raw_output=raw,
                candidate_file=candidate_file,
                command=tuple(command),
                exit_code=None,
                elapsed_seconds=elapsed,
                parsed_feedback=feedback,
            )
            return _with_progress(self, result)
        except LeanServerError as exc:
            # Covers both an outright server error and
            # ``LeanServerAmbiguousCompletion`` (diagnostics were published but
            # no conclusive completion signal arrived). The ambiguous case is
            # NOT retried in-place via ``server_timeout_retries`` — restarting
            # the server and re-running is unlikely to change Lean's signaling,
            # so we close the server and fall through to the authoritative
            # subprocess checker, which has a deterministic exit code.
            logger.warning("Lean server check failed; falling back to subprocess check", exc_info=True)
            self.close()
            if self.require_server:
                return self._server_failure_result(candidate_file, str(exc), command=command)
            return None

        elapsed = time.perf_counter() - started
        logger.info(
            "Lean server check completed: candidate_file=%s exit_code=%s elapsed=%.3fs",
            candidate_file,
            server_result.exit_code,
            elapsed,
        )

        raw = server_result.raw_output
        feedback = self.parse_feedback(raw)
        if server_result.exit_code != 0 and feedback.category == DiagnosticCategory.PROOF_ACCEPTED:
            feedback = ParsedFeedback(
                category=DiagnosticCategory.CHECKER_ERROR,
                message=f"Lean server reported failure without diagnostic output.",
                raw_output=raw,
            )

        feedback = self._finalize_feedback(feedback, raw, server_result.exit_code == 0)
        accepted = (
            server_result.exit_code == 0
            and feedback.category == DiagnosticCategory.PROOF_ACCEPTED
        )
        result = CheckResult(
            accepted=accepted,
            category=feedback.category if not accepted else DiagnosticCategory.PROOF_ACCEPTED,
            raw_output=raw,
            candidate_file=candidate_file,
            command=tuple(command),
            exit_code=server_result.exit_code,
            elapsed_seconds=elapsed,
            parsed_feedback=feedback,
        )
        return _with_progress(self, result)

    def _server_failure_result(
        self,
        candidate_file: Path,
        message: str,
        *,
        command: tuple[str, ...] | list[str] = (),
    ) -> CheckResult:
        raw = f"Lean server infrastructure failure: {message}"
        feedback = ParsedFeedback(
            category=DiagnosticCategory.CHECKER_ERROR,
            message=raw,
            raw_output=raw,
        )
        return _with_progress(
            self,
            CheckResult(
                accepted=False,
                category=DiagnosticCategory.CHECKER_ERROR,
                raw_output=raw,
                candidate_file=candidate_file,
                command=tuple(command),
                exit_code=None,
                parsed_feedback=feedback,
            ),
        )

    def _finalize_feedback(
        self,
        feedback: ParsedFeedback,
        raw_output: str,
        exit_zero: bool,
    ) -> ParsedFeedback:
        """Apply ``disallow_sorry`` and success-path normalization."""
        if self.disallow_sorry and contains_sorry_warning(raw_output):
            return ParsedFeedback(
                category=DiagnosticCategory.UNSOLVED_GOALS,
                message="Lean accepted the file but the declaration uses 'sorry'.",
                raw_output=raw_output,
            )
        if exit_zero and not contains_error_diagnostic(raw_output):
            return ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED,
                message="Proof accepted.",
                raw_output=raw_output,
            )
        return feedback


def _minimal_result(feedback: ParsedFeedback) -> CheckResult:
    return CheckResult(
        accepted=False,
        category=feedback.category,
        raw_output=feedback.raw_output,
        parsed_feedback=feedback,
    )


def _with_progress(adapter: LeanAdapter, result: CheckResult) -> CheckResult:
    return CheckResult(
        accepted=result.accepted,
        category=result.category,
        raw_output=result.raw_output,
        candidate_file=result.candidate_file,
        command=result.command,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
        parsed_feedback=result.parsed_feedback,
        progress=adapter.extract_progress(None, result),
    )
