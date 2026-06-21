"""Retrieval and context-summarization helpers for the controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ...agents.context import ContextSummarizer, SummarizationRequest
from .types import ControllerConfig, _ControllerRunState

if TYPE_CHECKING:
    from ...proof_system.base import ParsedFeedback, ProofTask
    from ...retrieval import RetrievalResult
    from .types import Retriever


logger = logging.getLogger(__name__)


def summarize_context(
    task: ProofTask,
    state: _ControllerRunState,
    context_summarizer: ContextSummarizer | None,
    previous_attempt: dict[str, Any] | None,
) -> Any:
    """Compress checker output and history into a short, actionable summary."""
    if context_summarizer is None or state.attempt_index == 0:
        return None
    try:
        return context_summarizer.summarize(
            SummarizationRequest(
                task=task,
                attempt_index=state.attempt_index,
                previous_attempt=previous_attempt,
                feedback_history=tuple(state.feedback_history),
                retrieved_results=state.current_retrieved,
            )
        )
    except Exception:
        logger.debug("Context summarization failed", exc_info=True)
        return None


def maybe_retrieve(
    task: ProofTask,
    state: _ControllerRunState,
    retriever: Retriever | None,
    config: ControllerConfig,
    *,
    is_first_iteration: bool,
) -> tuple[RetrievalResult, ...]:
    """Return relevant snippets when the configured trigger fires."""
    if retriever is None:
        return ()

    feedback: ParsedFeedback | None = None
    if is_first_iteration:
        if not config.retrieve_before_first_model_call:
            return ()
    else:
        if state.feedback_history:
            feedback = state.feedback_history[-1]
        if feedback is None or feedback.category not in config.retrieve_on_categories:
            return ()

    logger.debug(
        "Retrieving context: task_id=%s feedback_category=%s",
        task.task_id,
        feedback.category.value if feedback else None,
    )
    return retriever.retrieve(
        task=task,
        feedback=feedback,
        top_k=config.max_retrieval_results,
    )
