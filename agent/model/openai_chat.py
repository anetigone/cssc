"""OpenAI-compatible model adapter for generating proof actions."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from ..search.action import ActionCandidate, ActionGenerationRequest, ActionGenerator
from ..proof_system.base import ParsedFeedback, ProofTask


logger = logging.getLogger(__name__)


class ModelAdapterError(RuntimeError):
    """Raised when a model request cannot be completed or parsed."""


class ChatTransport(Protocol):
    """HTTP transport seam for tests and smoke runs."""

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """POST JSON and return decoded JSON."""


@dataclass(frozen=True)
class OpenAIChatConfig:
    """Configuration for OpenAI-compatible chat completions."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    max_tokens: int = 512
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "OpenAIChatConfig":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("OPENAI_MODEL", "")
        base_url = (
            os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        )
        if not api_key:
            raise ModelAdapterError("OPENAI_API_KEY is not set.")
        if not model:
            raise ModelAdapterError("OPENAI_MODEL is not set.")
        return cls(api_key=api_key, model=model, base_url=base_url)


class OpenAIChatActionGenerator(ActionGenerator):
    """Generate proof edits through an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        config: OpenAIChatConfig,
        *,
        transport: ChatTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibChatTransport()

    def generate(self, request: ActionGenerationRequest) -> Sequence[ActionCandidate]:
        url = _chat_completions_url(self.config.base_url)
        logger.debug(
            "Requesting chat completions: model=%s url=%s task_id=%s max_candidates=%d",
            self.config.model,
            url,
            request.task.task_id,
            request.max_candidates,
        )
        payload = {
            "model": self.config.model,
            "messages": _build_messages(request),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "n": request.max_candidates,
            **self.config.extra_body,
        }
        response = self.transport.post_json(
            url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
        )
        choices = response.get("choices")
        if not isinstance(choices, list):
            logger.error("Model response missing choices list: model=%s", self.config.model)
            raise ModelAdapterError("Model response is missing a choices list.")

        candidates: list[ActionCandidate] = []
        for index, choice in enumerate(choices[: request.max_candidates]):
            if not isinstance(choice, Mapping):
                continue
            content = _choice_content(choice)
            proof_text = _clean_proof_text(content)
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
        return tuple(candidates)


class UrllibChatTransport:
    """Small standard-library JSON transport."""

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
        try:
            logger.debug("POST model request: url=%s timeout=%s", url, timeout_seconds)
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.warning("Model endpoint returned HTTP error: url=%s status=%s", url, exc.code)
            raise ModelAdapterError(f"Model endpoint returned HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            logger.warning("Model endpoint unavailable: url=%s reason=%s", url, exc.reason)
            raise ModelAdapterError(f"Model endpoint is unavailable: {exc.reason}") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("Model endpoint returned invalid JSON: url=%s", url)
            raise ModelAdapterError("Model endpoint returned invalid JSON.") from exc
        if not isinstance(decoded, Mapping):
            logger.warning("Model endpoint returned non-object JSON: url=%s", url)
            raise ModelAdapterError("Model endpoint returned a non-object JSON payload.")
        return decoded


def _build_messages(request: ActionGenerationRequest) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You complete Lean 4 proof holes. Return only the Lean proof body "
                "that replaces the marker. Do not wrap it in markdown."
            ),
        },
        {
            "role": "user",
            "content": _build_user_prompt(request),
        },
    ]


def _build_user_prompt(request: ActionGenerationRequest) -> str:
    task = request.task
    feedback = request.previous_feedback
    parts = [
        f"Task id: {task.task_id}",
        f"Replace exactly this marker: {task.hole_marker}",
    ]
    meta_action = request.metadata.get("meta_action")
    if isinstance(meta_action, str):
        parts.append(f"Controller action: {meta_action}")
    encoded_state = request.metadata.get("encoded_state")
    if encoded_state is not None and hasattr(encoded_state, "to_prompt_context"):
        parts.extend(["Controller state:", str(encoded_state.to_prompt_context())])
    retrieved = request.metadata.get("retrieved_results") or ()
    if isinstance(retrieved, Sequence) and retrieved:
        parts.append("Retrieved Lean snippets:")
        for item in retrieved[:5]:
            name = getattr(item, "name", None)
            snippet = getattr(item, "snippet", None)
            if isinstance(name, str) and isinstance(snippet, str):
                parts.extend([f"- {name}", "```lean", snippet, "```"])
    parts.extend(
        [
        "Lean source template:",
        "```lean",
        task.source_template,
        "```",
        ]
    )
    if feedback:
        parts.extend(["Previous checker feedback:"])
        for item in feedback[-3:]:
            parts.append(f"- {item.category.value}: {item.message}")
    return "\n".join(parts)


def _choice_content(choice: Mapping[str, Any]) -> str:
    message = choice.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def _clean_proof_text(content: str) -> str:
    stripped = content.strip()
    fence = re.fullmatch(r"```(?:lean)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    return stripped


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"
