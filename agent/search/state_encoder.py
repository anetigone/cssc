"""Compact proof-state encoding for controller prompts and traces."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .budget import BudgetSnapshot
from ..proof_system.base import DiagnosticCategory, ParsedFeedback, ProofTask


_DECL_RE = re.compile(r"^\s*(?:theorem|lemma|def|example)\s+([A-Za-z_][\w'.]*)", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*import\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class EncodedProofState:
    """Small, serializable view of the current task and recent feedback."""

    task_id: str
    proof_system: str = "lean4"
    imports: tuple[str, ...] = ()
    declarations: tuple[str, ...] = ()
    proof_prefix: str = ""
    recent_error_category: DiagnosticCategory = DiagnosticCategory.UNKNOWN
    recent_error_message: str = ""
    goals: tuple[str, ...] = ()
    branch_history: tuple[DiagnosticCategory, ...] = ()
    remaining_budget: dict[str, int | float | str | None] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_prompt_context(self) -> str:
        """Render a terse text block suitable for a model prompt."""

        lines = [f"task_id: {self.task_id}", f"proof_system: {self.proof_system}"]
        if self.imports:
            lines.append("imports: " + ", ".join(self.imports))
        if self.declarations:
            lines.append("local_declarations: " + ", ".join(self.declarations))
        if self.recent_error_category != DiagnosticCategory.UNKNOWN:
            lines.append(f"recent_error: {self.recent_error_category.value}")
        if self.recent_error_message:
            lines.append(f"message: {self.recent_error_message}")
        if self.goals:
            lines.append("goals:")
            lines.extend(_indent(goal) for goal in self.goals)
        return "\n".join(lines)


def encode_proof_state(
    task: ProofTask,
    *,
    feedback_history: Sequence[ParsedFeedback] = (),
    budget: BudgetSnapshot | None = None,
    metadata: dict[str, Any] | None = None,
) -> EncodedProofState:
    """Build a compact controller-facing state from task metadata and feedback."""

    recent = feedback_history[-1] if feedback_history else None
    source_imports = tuple(task.metadata.get("source_imports") or ())
    imports = tuple(dict.fromkeys((*source_imports, *task.imports, *_IMPORT_RE.findall(task.source_template))))
    hole_index = task.source_template.find(task.hole_marker)
    proof_prefix = task.source_template[:hole_index] if hole_index >= 0 else task.source_template

    return EncodedProofState(
        task_id=task.task_id,
        proof_system=str(task.metadata.get("proof_system") or "lean4"),
        imports=imports,
        declarations=tuple(_DECL_RE.findall(task.source_template)),
        proof_prefix=proof_prefix,
        recent_error_category=recent.category if recent else DiagnosticCategory.UNKNOWN,
        recent_error_message=recent.message if recent else "",
        goals=recent.unsolved_goals if recent else (),
        branch_history=tuple(feedback.category for feedback in feedback_history),
        remaining_budget=_budget_dict(budget),
        metadata=dict(metadata or {}),
    )


def _budget_dict(budget: BudgetSnapshot | None) -> dict[str, int | float | str | None]:
    if budget is None:
        return {}
    return {
        "remaining_checks": budget.remaining_checks,
        "remaining_model_calls": budget.remaining_model_calls,
        "elapsed_seconds": budget.elapsed_seconds,
        "exhausted_reason": budget.exhausted_reason,
    }


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())
