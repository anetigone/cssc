"""Lightweight context-management agent for proof-search prompts.

The context summarizer is *not* a proof generator. Its only job is to take the
noisy, growing pile of checker output, feedback history, and retrieved snippets
and distill it into a short, actionable summary that the proof generator can
consume without its prompt ballooning on every retry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from ..proof_system.base import ParsedFeedback, ProofTask
from .chat_driver import ChatDriver
from .openai import ChatConfig, ChatTransport, ModelAdapterError, UrllibChatTransport, choice_content


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummarizationRequest:
    """Inputs for a context-summarization pass."""

    task: ProofTask
    attempt_index: int
    previous_attempt: Mapping[str, Any] | None = None
    feedback_history: tuple[ParsedFeedback, ...] = ()
    retrieved_results: tuple[Any, ...] = ()


@dataclass(frozen=True)
class SummarizationResult:
    """Compact context ready to be injected into a proof-generation prompt."""

    concise_error: str = ""
    relevant_history: tuple[str, ...] = ()
    retained_retrieved: tuple[str, ...] = ()
    strategy_hint: str = ""
    was_summarized: bool = False


class ContextSummarizer(Protocol):
    """Boundary used by the controller to compress retry context."""

    def summarize(self, request: SummarizationRequest) -> SummarizationResult:
        """Return a compact view of the current proof-search context."""
        ...


class ChatContextSummarizer:
    """Use a cheap chat model to compress checker feedback and history."""

    def __init__(
        self,
        config: ChatConfig,
        *,
        transport: ChatTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()
        self.driver = ChatDriver(
            config=config,
            transport=self.transport,
            tools=(),
            max_tool_rounds=0,
        )

    def summarize(self, request: SummarizationRequest) -> SummarizationResult:
        if not self._should_summarize(request):
            return SummarizationResult()

        messages = self._build_messages(request)
        logger.debug(
            "Summarizing proof-search context: task_id=%s attempt_index=%d",
            request.task.task_id,
            request.attempt_index,
        )
        response = self.driver.complete(messages, final_n=1, allow_tools=False)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            logger.warning(
                "Context summarizer returned no choices: task_id=%s",
                request.task.task_id,
            )
            return SummarizationResult()

        content = choice_content(choices[0])
        parsed = self._parse_summary(content)
        logger.debug(
            "Context summarized: task_id=%s concise_error_chars=%d strategy_hint_chars=%d",
            request.task.task_id,
            len(parsed.concise_error),
            len(parsed.strategy_hint),
        )
        return parsed

    def _should_summarize(self, request: SummarizationRequest) -> bool:
        if request.attempt_index == 0:
            return False
        if request.previous_attempt is None and not request.feedback_history:
            return False
        return True

    def _build_messages(
        self, request: SummarizationRequest
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are a terse proof-search assistant. Your job is to compress "
                    "Lean checker output and retry history into a short, actionable "
                    "summary for another model that will rewrite the proof. "
                    "Be specific about the failing subargument, tactic, or type mismatch, "
                    "and suggest a concrete repair direction. Do not write the proof itself. "
                    "Respond in the exact JSON format requested by the user."
                ),
            },
            {"role": "user", "content": self._build_user_prompt(request)},
        ]

    def _build_user_prompt(self, request: SummarizationRequest) -> str:
        parts = [
            f"Task id: {request.task.task_id}",
            f"Attempt index: {request.attempt_index}",
        ]

        previous = request.previous_attempt
        if previous is not None:
            proof_text = previous.get("proof_text")
            raw_output = previous.get("raw_output")
            category = previous.get("category")
            if isinstance(proof_text, str) and proof_text.strip():
                parts.extend(
                    ["Previous proof body:", "```lean", proof_text.strip(), "```"]
                )
            if isinstance(category, str):
                parts.append(f"Checker category: {category}")
            if isinstance(raw_output, str) and raw_output.strip():
                parts.extend(
                    ["Raw checker output:", "```text", raw_output.strip(), "```"]
                )

        if request.feedback_history:
            parts.append("Prior checker feedback:")
            for item in request.feedback_history[-3:]:
                line = f"- {item.category.value}"
                if item.message:
                    line += f": {item.message}"
                if item.line is not None:
                    line += f" (line {item.line})"
                parts.append(line)
                if item.unsolved_goals:
                    parts.append("  unsolved goals:")
                    for goal in item.unsolved_goals[:2]:
                        parts.extend(["  ```lean", _indent(goal), "  ```"])

        if request.retrieved_results:
            parts.append("Retrieved snippets:")
            for item in request.retrieved_results[:3]:
                name = getattr(item, "name", None)
                snippet = getattr(item, "snippet", None)
                if isinstance(name, str) and isinstance(snippet, str):
                    parts.extend([f"- {name}", "```lean", snippet, "```"])

        parts.append(
            "Now respond with a JSON object containing exactly these keys:\n"
            "- concise_error: one or two sentences describing the precise failure\n"
            "- relevant_history: a list of the most important earlier feedback lines\n"
            "- retained_retrieved: a list of retrieved snippet names still worth keeping\n"
            "- strategy_hint: concrete advice for the next proof attempt\n"
            "Keep the total response under 400 tokens."
        )
        return "\n".join(parts)

    def _parse_summary(self, content: str) -> SummarizationResult:
        text = content.strip()
        fence = _extract_json_fence(text)
        if fence:
            text = fence
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug(
                "Context summarizer returned non-JSON content; using as concise_error"
            )
            return SummarizationResult(
                concise_error=text,
                was_summarized=True,
            )

        if not isinstance(data, dict):
            return SummarizationResult(concise_error=text, was_summarized=True)

        return SummarizationResult(
            concise_error=_string_from(data, "concise_error", text),
            relevant_history=_tuple_from(data, "relevant_history"),
            retained_retrieved=_tuple_from(data, "retained_retrieved"),
            strategy_hint=_string_from(data, "strategy_hint", ""),
            was_summarized=True,
        )


def _extract_json_fence(text: str) -> str | None:
    """Pull a JSON object out of a markdown code fence if present."""
    start = text.find("```json")
    if start == -1:
        start = text.find("```")
    if start == -1:
        return None
    end = text.find("```", start + 3)
    if end == -1:
        return None
    return text[start + 3 : end].removeprefix("json").strip()


def _string_from(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str):
        return value.strip()
    return default


def _tuple_from(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, str):
        return (value.strip(),)
    return ()


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


# Backwards-compatible alias for callers that used an earlier name.
OpenAIChatContextSummarizer = ChatContextSummarizer
