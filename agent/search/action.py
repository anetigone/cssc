"""Action generation boundary for proof-search controllers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ..proof_system.base import CandidateEdit, ParsedFeedback, ProofTask


class ActionGenerationError(RuntimeError):
    """Typed failure to produce actions, distinct from a deliberate empty set."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.metadata = dict(metadata or {})


RETRYABLE_GENERATION_FAILURES = frozenset(
    {
        "model_output_truncated",
        # Some OpenAI-compatible providers neither expose reasoning-token
        # details nor return a reliable length finish reason. An empty,
        # successfully decoded response is still a model-generation failure,
        # and another budgeted request can recover it.
        "empty_model_output",
    }
)


def is_retryable_generation_failure(exc: ActionGenerationError) -> bool:
    """Return whether another budgeted model request can recover the failure."""
    return exc.reason in RETRYABLE_GENERATION_FAILURES


@dataclass(frozen=True)
class ActionCandidate:
    """One model- or heuristic-proposed proof edit."""

    proof_text: str
    action: str = "model_complete"
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_edit(self, *, parent_node_id: str | None = None) -> CandidateEdit:
        metadata = dict(self.metadata)
        if self.score is not None:
            metadata["score"] = self.score
        return CandidateEdit(
            text=self.proof_text,
            action=self.action,
            parent_node_id=parent_node_id,
            metadata=metadata,
        )


@dataclass(frozen=True)
class ActionGenerationRequest:
    """Context given to a candidate generator."""

    task: ProofTask
    attempt_index: int
    previous_feedback: tuple[ParsedFeedback, ...] = ()
    max_candidates: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


class ActionGenerator(Protocol):
    """Minimal interface for plugging in a model later."""

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        """Return proof edits to try for this request."""


class StaticActionGenerator:
    """Deterministic generator useful for tests and smoke runs."""

    def __init__(self, candidates: Sequence[str | ActionCandidate]) -> None:
        self._candidates = tuple(_coerce_candidate(candidate) for candidate in candidates)

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        return self._candidates[: request.max_candidates]


def _coerce_candidate(candidate: str | ActionCandidate) -> ActionCandidate:
    if isinstance(candidate, ActionCandidate):
        return candidate
    return ActionCandidate(proof_text=candidate, action="static")
