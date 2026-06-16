"""Small deterministic proof-snippet proposers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .action import ActionCandidate, ActionGenerationRequest


@dataclass(frozen=True)
class ProofSnippet:
    """Reusable Lean proof snippet with optional metadata."""

    text: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class CandidateLibraryGenerator:
    """Return a fixed library of snippets as action candidates."""

    def __init__(self, snippets: Sequence[str | ProofSnippet]) -> None:
        self._snippets = tuple(_coerce_snippet(snippet) for snippet in snippets)

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        candidates = [
            ActionCandidate(
                proof_text=snippet.text,
                action="library",
                score=snippet.score,
                metadata=snippet.metadata,
            )
            for snippet in self._snippets
        ]
        return candidates[: request.max_candidates]


def _coerce_snippet(snippet: str | ProofSnippet) -> ProofSnippet:
    if isinstance(snippet, ProofSnippet):
        return snippet
    return ProofSnippet(text=snippet)
