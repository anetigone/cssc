"""Agent roles that propose Lean proof-hole completions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Sequence

from ..proof_system.base import ParsedFeedback, ProofTask
from ..search.action import ActionCandidate, ActionGenerationRequest, ActionGenerator
from .chat_driver import ChatDriver
from .openai import (
    ChatConfig,
    ChatTransport,
    ModelAdapterError,
    UrllibChatTransport,
    chat_completions_url,
    choice_content,
)
from .tools import Tool


logger = logging.getLogger(__name__)


class ChatActionGenerator(ActionGenerator):
    """Generate proof edits through a chat-completion endpoint."""

    def __init__(
        self,
        config: ChatConfig,
        *,
        transport: ChatTransport | None = None,
        tools: Sequence[Tool] | None = None,
        max_tool_rounds: int = 5,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()
        self.driver = ChatDriver(
            config=config,
            transport=self.transport,
            tools=tools or (),
            max_tool_rounds=max_tool_rounds,
        )

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        url = chat_completions_url(self.config.base_url)
        logger.debug(
            "Requesting chat completions: model=%s url=%s task_id=%s max_candidates=%d",
            self.config.model,
            url,
            request.task.task_id,
            request.max_candidates,
        )
        messages: list[dict[str, Any]] = list(_build_messages(request))
        response = self.driver.complete(messages, final_n=request.max_candidates)
        choices = response.get("choices")
        if not isinstance(choices, list):
            logger.error("Model response missing choices list: model=%s", self.config.model)
            raise ModelAdapterError("Model response is missing a choices list.")

        candidates: list[ActionCandidate] = []
        for index, choice in enumerate(choices[: request.max_candidates]):
            if not isinstance(choice, Mapping):
                continue
            proof_text = _clean_proof_text(choice_content(choice))
            if not proof_text:
                continue
            candidates.append(
                ActionCandidate(
                    proof_text=proof_text,
                    action="openai_chat",
                    metadata={
                        "model": self.config.model,
                        "choice_index": index,
                        "finish_reason": choice.get("finish_reason"),
                    },
                )
            )
        logger.info(
            "Generated model candidates: model=%s task_id=%s candidates=%d",
            self.config.model,
            request.task.task_id,
            len(candidates),
        )
        if not candidates:
            logger.warning(
                "Model response produced no proof candidates: model=%s task_id=%s response=%s",
                self.config.model,
                request.task.task_id,
                json.dumps(response, ensure_ascii=False, default=str),
            )
        return tuple(candidates)


def _build_messages(request: ActionGenerationRequest) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You iteratively complete Lean 4 proof holes. Return only the full Lean proof "
                "body that replaces the marker; do not wrap it in markdown. On later attempts, "
                "repair the previous proof using every compiler diagnostic instead of starting "
                "over or replacing a substantial proof with a generic tactic. Use the available "
                "Lean environment tools when unsure about modules or library names."
            ),
        },
        {"role": "user", "content": _build_user_prompt(request)},
    ]


def _build_user_prompt(request: ActionGenerationRequest) -> str:
    task = request.task
    feedback = request.previous_feedback
    parts = [
        f"Task id: {task.task_id}",
        f"Replace exactly this marker: {task.hole_marker}",
    ]
    problem = task.metadata.get("natural_language_problem")
    if isinstance(problem, str) and problem.strip():
        parts.extend(["Natural-language problem statement:", problem.strip()])
    informal_proof = task.metadata.get("natural_language_proof")
    if isinstance(informal_proof, str) and informal_proof.strip():
        parts.extend(
            [
                "Candidate natural-language proof to preserve when it is mathematically sound:",
                informal_proof.strip(),
            ]
        )
    meta_action = request.metadata.get("meta_action")
    if isinstance(meta_action, str):
        parts.append(f"Controller action: {meta_action}")
    encoded_state = request.metadata.get("encoded_state")
    if encoded_state is not None and hasattr(encoded_state, "to_prompt_context"):
        parts.extend(["Controller state:", str(encoded_state.to_prompt_context())])
    previous_attempt = request.metadata.get("previous_attempt")
    has_previous_attempt = isinstance(previous_attempt, Mapping)
    if has_previous_attempt:
        previous_proof = previous_attempt.get("proof_text")
        raw_output = previous_attempt.get("raw_output")
        if isinstance(previous_proof, str) and previous_proof.strip():
            parts.extend(
                ["Previous proof body to revise:", "```lean", previous_proof, "```"]
            )
        if isinstance(raw_output, str) and raw_output.strip():
            parts.extend(
                [
                    "Complete Lean compiler output from that proof:",
                    "```text",
                    raw_output,
                    "```",
                ]
            )
    retrieved = request.metadata.get("retrieved_results") or ()
    if isinstance(retrieved, Sequence) and retrieved:
        parts.append("Retrieved Lean snippets:")
        for item in retrieved[:5]:
            name = getattr(item, "name", None)
            snippet = getattr(item, "snippet", None)
            if isinstance(name, str) and isinstance(snippet, str):
                parts.extend([f"- {name}", "```lean", snippet, "```"])
    parts.extend(["Lean source template:", "```lean", task.source_template, "```"])
    if feedback:
        parts.extend(["Previous checker feedback:"])
        # If the previous attempt's full output is already shown above, the
        # most recent feedback entry repeats the same diagnosis; keep the
        # older history instead.
        feedback_slice = feedback[:-1] if has_previous_attempt else feedback[-3:]
        for item in feedback_slice:
            parts.append(_feedback_line(item))
    return "\n".join(parts)


def _feedback_line(feedback: ParsedFeedback) -> str:
    return f"- {feedback.category.value}: {feedback.message}"


def _clean_proof_text(content: str) -> str:
    stripped = content.strip()
    fence = re.fullmatch(r"```(?:lean)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    return stripped


# Backwards-compatible alias for code that still uses the old, longer name.
OpenAIChatActionGenerator = ChatActionGenerator
