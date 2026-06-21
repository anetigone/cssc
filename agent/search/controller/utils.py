"""Small pure helpers used by the proof-search controller."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...proof_system.base import CandidateEdit
from ...retrieval import RetrievalResult
from .types import _ControllerRunState


def _proof_phase(state: _ControllerRunState) -> str:
    """Expose the loop's intent for prompts and traces."""
    return "propose" if not state.attempts else "retry"


def _edit_with_controller_metadata(
    edit: CandidateEdit,
    *,
    proof_phase: str,
    retrieved: tuple[RetrievalResult, ...],
) -> CandidateEdit:
    metadata = dict(edit.metadata)
    metadata["proof_phase"] = proof_phase
    if retrieved:
        metadata["retrieved_results"] = tuple(
            _retrieval_payload(item) for item in retrieved
        )
    return replace(edit, metadata=metadata)


def _retrieval_payload(result: RetrievalResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "source_path": result.source_path,
        "start_line": result.start_line,
        "snippet": result.snippet,
        "score": result.score,
        "metadata": result.metadata,
    }
