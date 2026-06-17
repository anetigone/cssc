"""OpenAI-compatible chat transport shared by agent roles."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


logger = logging.getLogger(__name__)


class ModelAdapterError(RuntimeError):
    """Raised when a model request cannot be completed or parsed."""


class ChatTransport(Protocol):
    """HTTP transport boundary for tests, smoke runs, and agent roles."""

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """POST JSON and return decoded JSON."""
        ...


@dataclass(frozen=True)
class OpenAIChatConfig:
    """Configuration for OpenAI-compatible chat completions."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    max_tokens: int = 4096
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
        timeout_seconds = _float_from_env("OPENAI_TIMEOUT_SECONDS", cls.timeout_seconds)
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )


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
        except (TimeoutError, socket.timeout) as exc:
            logger.warning("Model endpoint timed out: url=%s timeout=%s", url, timeout_seconds)
            raise ModelAdapterError(f"Model endpoint timed out after {timeout_seconds}s.") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("Model endpoint returned invalid JSON: url=%s", url)
            raise ModelAdapterError("Model endpoint returned invalid JSON.") from exc
        if not isinstance(decoded, Mapping):
            logger.warning("Model endpoint returned non-object JSON: url=%s", url)
            raise ModelAdapterError("Model endpoint returned a non-object JSON payload.")
        return decoded


def _float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ModelAdapterError(f"{name} must be a number.") from exc


def chat_completions_url(base_url: str) -> str:
    """Return the chat completions endpoint for an OpenAI-compatible base URL."""

    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def choice_content(choice: Mapping[str, Any]) -> str:
    """Extract text content from a chat-completions choice."""

    message = choice.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def first_choice_content(response: Mapping[str, Any]) -> str:
    """Extract the first choice content from a chat-completions response."""

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelAdapterError("Model response is missing choices.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ModelAdapterError("Model choice is not an object.")
    content = choice_content(first)
    if not content:
        raise ModelAdapterError("Model choice does not contain text content.")
    return content


def parse_json_object(content: str, *, context: str = "Model response") -> Mapping[str, Any]:
    """Parse a JSON object, accepting an optional fenced JSON block."""

    stripped = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ModelAdapterError(f"{context} is not valid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise ModelAdapterError(f"{context} JSON must be an object.")
    return decoded
