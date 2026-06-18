"""Lean 4 implementation of the proof-system adapter."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..utils import resolve_executable
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
from .lean_server import LeanServerClient, LeanServerError, LeanServerTimeout


_LOCATION_RE = re.compile(r":(?P<line>\d+):(?P<column>\d+):\s+(?:error|warning):")
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
    ) -> None:
        self.project_root = Path(project_root).resolve() if project_root else None
        self.prefer_lake = prefer_lake
        self.disallow_sorry = disallow_sorry
        self.lean_executable = resolve_executable(lean_executable, "lean")
        self.lake_executable = resolve_executable(lake_executable, "lake")
        self.use_server = use_server
        self.server_startup_timeout_seconds = server_startup_timeout_seconds
        self.server_timeout_retries = server_timeout_retries
        self._server: LeanServerClient | None = None
        logger.debug(
            "Initialized LeanAdapter: project_root=%s prefer_lake=%s disallow_sorry=%s lean=%s lake=%s use_server=%s",
            self.project_root,
            self.prefer_lake,
            self.disallow_sorry,
            self.lean_executable,
            self.lake_executable,
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

        started = time.perf_counter()
        logger.debug(
            "Running Lean checker: command=%s cwd=%s timeout=%s",
            command,
            self.project_root or candidate_file.parent,
            budget_slice.timeout_seconds,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=self.project_root or candidate_file.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=budget_slice.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            logger.warning(
                "Lean checker timed out: candidate_file=%s timeout=%s elapsed=%.3f",
                candidate_file,
                budget_slice.timeout_seconds,
                elapsed,
            )
            raw = _combine_output(exc.stdout, exc.stderr)
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

        elapsed = time.perf_counter() - started
        raw = _combine_output(completed.stdout, completed.stderr)
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

        if self.disallow_sorry and _contains_sorry_warning(raw):
            logger.info("Rejecting Lean candidate because it uses sorry: candidate_file=%s", candidate_file)
            feedback = ParsedFeedback(
                category=DiagnosticCategory.UNSOLVED_GOALS,
                message="Lean accepted the file but the declaration uses 'sorry'.",
                raw_output=raw,
            )
        elif completed.returncode == 0 and not _contains_error_diagnostic(raw):
            feedback = ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED,
                message="Proof accepted.",
                raw_output=raw,
            )

        accepted = (
            completed.returncode == 0
            and feedback.category == DiagnosticCategory.PROOF_ACCEPTED
        )

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
        normalized = raw_output.lower()
        line, column = _first_location(raw_output)

        if not raw_output.strip():
            return ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED,
                message="Proof accepted.",
                raw_output=raw_output,
            )
        if "no default toolchain configured" in normalized or "toolchain" in normalized and "not installed" in normalized:
            category = DiagnosticCategory.TOOL_UNAVAILABLE
        elif "unknown identifier" in normalized or "unknown constant" in normalized:
            category = DiagnosticCategory.UNKNOWN_IDENTIFIER
        elif "type mismatch" in normalized or "application type mismatch" in normalized:
            category = DiagnosticCategory.TYPE_MISMATCH
        elif "unsolved goals" in normalized or "goals unsolved" in normalized:
            category = DiagnosticCategory.UNSOLVED_GOALS
        elif "tactic" in normalized and ("failed" in normalized or "unsolved" in normalized):
            category = DiagnosticCategory.TACTIC_FAILED
        elif "failed to synthesize" in normalized:
            category = DiagnosticCategory.TYPE_MISMATCH
        elif "unexpected token" in normalized or "parser" in normalized:
            category = DiagnosticCategory.PARSER_ERROR
        elif "termination" in normalized or "failed to prove termination" in normalized:
            category = DiagnosticCategory.TERMINATION_ISSUE
        elif "invalid" in normalized and ("theorem" in normalized or "declaration" in normalized):
            category = DiagnosticCategory.INVALID_REFERENCE
        elif "error:" in normalized:
            category = DiagnosticCategory.CHECKER_ERROR
        elif _contains_sorry_warning(raw_output):
            category = DiagnosticCategory.UNSOLVED_GOALS
        else:
            category = DiagnosticCategory.UNKNOWN

        return ParsedFeedback(
            category=category,
            message=_first_meaningful_line(raw_output),
            line=line,
            column=column,
            unsolved_goals=_extract_goal_blocks(raw_output),
            raw_output=raw_output,
        )

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
        if self.prefer_lake and self.lake_executable and self._has_lake_project():
            return [self.lake_executable, "env", "lean", str(candidate_file)]
        if self.lean_executable:
            return [self.lean_executable, str(candidate_file)]
        if self.lake_executable and self._has_lake_project():
            return [self.lake_executable, "env", "lean", str(candidate_file)]
        return None

    def _build_server_command(self) -> list[str] | None:
        if self.prefer_lake and self.lake_executable and self._has_lake_project():
            return [self.lake_executable, "env", "lean", "--server"]
        if self.lean_executable:
            return [self.lean_executable, "--server"]
        if self.lake_executable and self._has_lake_project():
            return [self.lake_executable, "env", "lean", "--server"]
        return None

    def start_service(self, *, timeout_seconds: float = 10.0) -> bool:
        """Start a persistent Lean language server for repeated checks."""

        if not self.use_server:
            return False
        if self._server is not None and self._server.is_alive():
            return True
        command = self._build_server_command()
        if command is None:
            return False
        server: LeanServerClient | None = None
        try:
            server = LeanServerClient(
                command,
                cwd=self.project_root,
                root=self.project_root,
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
        except LeanServerError:
            logger.warning("Lean server check failed; falling back to subprocess check", exc_info=True)
            self.close()
            return None

        elapsed = time.perf_counter() - started
        raw = server_result.raw_output
        feedback = self.parse_feedback(raw)
        if server_result.exit_code != 0 and feedback.category == DiagnosticCategory.PROOF_ACCEPTED:
            feedback = ParsedFeedback(
                category=DiagnosticCategory.CHECKER_ERROR,
                message=f"Lean server reported failure without diagnostic output.",
                raw_output=raw,
            )

        if self.disallow_sorry and _contains_sorry_warning(raw):
            logger.info("Rejecting Lean candidate because it uses sorry: candidate_file=%s", candidate_file)
            feedback = ParsedFeedback(
                category=DiagnosticCategory.UNSOLVED_GOALS,
                message="Lean accepted the file but the declaration uses 'sorry'.",
                raw_output=raw,
            )
        elif server_result.exit_code == 0 and not _contains_error_diagnostic(raw):
            feedback = ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED,
                message="Proof accepted.",
                raw_output=raw,
            )

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

    def _has_lake_project(self) -> bool:
        if self.project_root is None:
            return False
        return (self.project_root / "lakefile.lean").exists() or (
            self.project_root / "lakefile.toml"
        ).exists()


def _combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value

    parts = [part for part in (as_text(stdout), as_text(stderr)) if part]
    return "\n".join(parts).strip()




def _contains_sorry_warning(raw_output: str) -> bool:
    normalized = raw_output.lower()
    return bool(
        re.search(r"\bdeclaration\b.*\buses\b.*\bsorry\b", normalized)
        or re.search(r"\bwarning\b.*\bsorry\b", normalized)
        or re.search(r"\bsorry\b.*\baxiom\b", normalized)
    )


def _contains_error_diagnostic(raw_output: str) -> bool:
    return bool(re.search(r"\berror:", raw_output.lower()))


def _first_location(raw_output: str) -> tuple[int | None, int | None]:
    match = _LOCATION_RE.search(raw_output)
    if not match:
        return None, None
    return int(match.group("line")), int(match.group("column"))


def _first_meaningful_line(raw_output: str) -> str:
    for line in raw_output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _extract_goal_blocks(raw_output: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    capture = False
    for line in raw_output.splitlines():
        if "unsolved goals" in line.lower():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            capture = True
            continue
        if capture:
            if line.strip().startswith("error:") and current:
                blocks.append("\n".join(current).strip())
                current = []
                capture = False
            else:
                current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return tuple(block for block in blocks if block)


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
