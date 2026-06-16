"""Feedback-driven repair candidates for Lean proof holes."""

from __future__ import annotations

from typing import Sequence

from .action import ActionCandidate, ActionGenerationRequest
from ..proof_system.base import DiagnosticCategory, ParsedFeedback


class FeedbackRepairGenerator:
    """Generate simple repair attempts from normalized checker feedback."""

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        feedback = request.previous_feedback[-1] if request.previous_feedback else None
        candidates = _repair_candidates(feedback)
        return candidates[: request.max_candidates]


def _repair_candidates(feedback: ParsedFeedback | None) -> tuple[ActionCandidate, ...]:
    if feedback is None:
        return (
            _candidate("simp", "no_feedback_default", 0.25),
            _candidate("trivial", "no_feedback_default", 0.2),
        )

    category = feedback.category
    if category == DiagnosticCategory.PARSER_ERROR:
        return (
            _candidate("simp", "parser_error_simplify", 0.35),
            _candidate("trivial", "parser_error_trivial", 0.25),
        )
    if category in {DiagnosticCategory.UNKNOWN_IDENTIFIER, DiagnosticCategory.INVALID_REFERENCE}:
        return (
            _candidate("simp", "unknown_identifier_remove_reference", 0.35),
            _candidate("first | trivial | simp", "unknown_identifier_safe_fallback", 0.3),
        )
    if category == DiagnosticCategory.TYPE_MISMATCH:
        return (
            _candidate("simp", "type_mismatch_simplify", 0.3),
            _candidate("try contradiction\n  try trivial\n  simp", "type_mismatch_basic_closure", 0.25),
        )
    if category in {DiagnosticCategory.UNSOLVED_GOALS, DiagnosticCategory.TACTIC_FAILED}:
        goal_text = "\n\n".join(feedback.unsolved_goals)
        metadata = {"repair_reason": "unsolved_goal"}
        if goal_text:
            metadata["goal_excerpt"] = goal_text[:500]
        return (
            ActionCandidate("simp", action="repair", score=0.35, metadata=metadata),
            ActionCandidate("first | trivial | simp", action="repair", score=0.3, metadata=metadata),
        )
    if category == DiagnosticCategory.TIMEOUT:
        return (
            _candidate("simp", "timeout_short_candidate", 0.25),
        )
    return (
        _candidate("simp", "generic_repair", 0.2),
    )


def _candidate(text: str, reason: str, score: float) -> ActionCandidate:
    return ActionCandidate(
        proof_text=text,
        action="repair",
        score=score,
        metadata={"repair_reason": reason},
    )
