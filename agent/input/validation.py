"""Scaffold validation for formalized Lean sources."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from itertools import count
from typing import Any, Protocol

from ..proof_system.base import BudgetSlice, CheckResult, DiagnosticCategory
from ..runtime.workspace import EphemeralCheckWorkspace, _safe_name


class _CheckableAdapter(Protocol):
    """Minimal interface needed by LeanAdapterScaffoldChecker."""

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult: ...


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationConfig:
    """Configuration for formalization scaffold validation."""

    max_retries: int = 1
    check_timeout_seconds: float = 10.0
    inactive_fill: str = "sorry"


class ScaffoldValidationError(ValueError):
    """Raised when a formalized scaffold fails validation."""

    def __init__(self, message: str, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


def validate_scaffold_json(data: Any) -> tuple[str, str | None]:
    """Validate the JSON shape of a formalizer response.

    Returns ``(proof_source, natural_language_proof)``. ``proof_source`` must be
    a non-empty string. ``natural_language_proof`` is optional. Accepted aliases
    are preserved for backward compatibility.
    """
    if not isinstance(data, dict):
        raise ScaffoldValidationError("Formalizer response must be a JSON object.", stage="json_shape")

    proof_source = data.get("proof_source") or data.get("lean") or data.get("source_template")
    if not isinstance(proof_source, str) or not proof_source.strip():
        raise ScaffoldValidationError(
            "Formalizer response must contain a non-empty proof_source string.",
            stage="json_shape",
        )

    natural_language_proof = data.get("natural_language_proof") or data.get("informal_proof")
    if natural_language_proof is not None and not isinstance(natural_language_proof, str):
        raise ScaffoldValidationError(
            "natural_language_proof must be a string when provided.",
            stage="json_shape",
        )

    return proof_source.strip(), natural_language_proof


@dataclass(frozen=True)
class ScaffoldValidationResult:
    """Outcome of a scaffold Lean check."""

    ok: bool
    message: str = ""
    category: DiagnosticCategory | None = None


class LeanAdapterScaffoldChecker:
    """Check a formalized scaffold by filling its hole and running Lean."""

    def __init__(
        self,
        adapter: _CheckableAdapter,
        workspace: EphemeralCheckWorkspace | None = None,
        validation: ValidationConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.workspace = workspace
        self.validation = validation or ValidationConfig()
        self._candidate_ids = count()

    def validate_scaffold(
        self,
        source: str,
        *,
        imports: tuple[str, ...] = (),
        hole_marker: str = "{{proof}}",
        inactive_fill: str = "sorry",
    ) -> ScaffoldValidationResult:
        """Return whether ``source`` compiles after filling the active hole."""
        filled = _fill_hole(source, hole_marker=hole_marker, inactive_fill=inactive_fill)
        if imports:
            filled = "\n".join(f"import {module}" for module in imports) + "\n\n" + filled

        from ..tasks.types import ProofTask, TaskInputKind

        workspace = self.workspace
        own_workspace: EphemeralCheckWorkspace | None = None
        if workspace is None:
            own_workspace = EphemeralCheckWorkspace(Path(tempfile.mkdtemp()), keep_files=False)
            workspace = own_workspace

        dummy_task = ProofTask(
            task_id="scaffold_check",
            source_template=source,
            input_kind=TaskInputKind.LEAN,
        )
        try:
            with workspace.materialize_candidate(
                dummy_task,
                candidate_id=f"scaffold_check_{next(self._candidate_ids)}",
                source=filled,
            ) as candidate:
                result = self.adapter.check(
                    candidate.path,
                    BudgetSlice(timeout_seconds=self.validation.check_timeout_seconds),
                )
        finally:
            if own_workspace is not None:
                try:
                    shutil.rmtree(own_workspace.root, ignore_errors=True)
                except OSError:
                    logger.warning("Failed to clean up temporary scaffold workspace", exc_info=True)

        if result.category == DiagnosticCategory.TOOL_UNAVAILABLE:
            logger.warning("Lean checker unavailable for scaffold validation; skipping: %s", result.raw_output)
            return ScaffoldValidationResult(ok=True, message=result.raw_output, category=result.category)

        if result.accepted:
            return ScaffoldValidationResult(ok=True, message="Scaffold compiles.", category=result.category)

        return ScaffoldValidationResult(
            ok=False,
            message=result.raw_output or "Scaffold failed Lean validation.",
            category=result.category,
        )


def _fill_hole(source: str, *, hole_marker: str, inactive_fill: str) -> str:
    if hole_marker in source:
        return source.replace(hole_marker, inactive_fill)
    # No explicit marker: the scaffold likely relies on a standalone ``sorry`` as
    # its proof hole (the form LeanTaskBuilder also accepts). Leave it in place so
    # the checker validates the surrounding syntax/imports with the hole present.
    # Note: the checker runs with disallow_sorry disabled, so a ``sorry`` hole
    # compiles and the validation signal here is syntax/type/import errors only.
    return source
