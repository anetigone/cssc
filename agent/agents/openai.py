"""OpenAI-compatible chat transport shared by agent roles."""

from __future__ import annotations

import json
import http.client
import logging
import os
import re
import socket
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


logger = logging.getLogger(__name__)


class ModelAdapterError(RuntimeError):
    """Raised when a model request cannot be completed or parsed."""


def normalized_token_usage(response: Mapping[str, Any]) -> dict[str, int]:
    """Normalize provider usage while excluding hidden reasoning from output cost.

    OpenAI-compatible providers do not agree on whether they expose reasoning
    details. When they do, ``completion_tokens`` normally includes those hidden
    tokens; the comparable visible-output count is therefore completion minus
    reasoning. Providers without reasoning details retain their reported
    completion/output count unchanged.
    """
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "provider_completion_tokens": 0,
            "provider_total_tokens": 0,
        }

    input_tokens = _usage_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion_tokens = _usage_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    details = usage.get("completion_tokens_details")
    if not isinstance(details, Mapping):
        details = usage.get("output_tokens_details")
    reasoning_tokens = (
        _usage_int(details.get("reasoning_tokens"))
        if isinstance(details, Mapping)
        else 0
    )
    # Be conservative with non-standard providers whose detail count is
    # inconsistent with their completion total.
    hidden_reasoning = min(reasoning_tokens, completion_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": completion_tokens - hidden_reasoning,
        "reasoning_tokens": reasoning_tokens,
        "provider_completion_tokens": completion_tokens,
        "provider_total_tokens": _usage_int(usage.get("total_tokens")),
    }


def merge_token_usage(*usages: Mapping[str, Any]) -> dict[str, int]:
    """Add normalized usage records across all requests in one tool loop."""
    keys = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "provider_completion_tokens",
        "provider_total_tokens",
    )
    return {
        key: sum(_usage_int(usage.get(key)) for usage in usages)
        for key in keys
    }


def _usage_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


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
class ChatConfig:
    """Configuration for OpenAI-compatible chat completions."""

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    max_tokens: int = 16384
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        *,
        timeout_seconds: float,
        max_tokens: int = 16384,
        model: str | None = None,
        temperature: float | None = None,
    ) -> "ChatConfig":
        """Build a config from environment, with optional per-call overrides.

        ``model`` and ``temperature`` default to ``None``, meaning "use the
        environment / dataclass default", so existing callers are unaffected.
        Passing a value overrides it for this config only.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "")
        resolved_model = model or os.environ.get("OPENAI_MODEL", "")
        base_url = (
            os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        )
        if not api_key:
            raise ModelAdapterError("OPENAI_API_KEY is not set.")
        if not resolved_model:
            raise ModelAdapterError("OPENAI_MODEL is not set.")
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "model": resolved_model,
            "base_url": base_url,
            "timeout_seconds": timeout_seconds,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return cls(**kwargs)


class UrllibChatTransport:
    """Small standard-library JSON transport."""

    def __init__(self, *, max_retries: int = 2, retry_backoff_seconds: float = 1.0) -> None:
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
        request_id = uuid.uuid4().hex[:8]
        transient_errors = (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
            socket.timeout,
        )
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                logger.debug(
                    "Model request started: request_id=%s url=%s timeout=%s attempt=%d/%d request_bytes=%d",
                    request_id,
                    url,
                    timeout_seconds,
                    attempt + 1,
                    self.max_retries + 1,
                    len(data),
                )
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw_body = response.read()
                    status = getattr(response, "status", None)
                elapsed = time.perf_counter() - started
                body = raw_body.decode("utf-8")
                logger.debug(
                    "Model request completed: request_id=%s url=%s attempt=%d/%d status=%s elapsed=%.3fs response_bytes=%d",
                    request_id,
                    url,
                    attempt + 1,
                    self.max_retries + 1,
                    status,
                    elapsed,
                    len(raw_body),
                )
                break
            except urllib.error.HTTPError as exc:
                elapsed = time.perf_counter() - started
                body = exc.read().decode("utf-8", errors="replace")
                logger.warning(
                    "Model request failed with HTTP error: request_id=%s url=%s status=%s elapsed=%.3fs",
                    request_id,
                    url,
                    exc.code,
                    elapsed,
                )
                raise ModelAdapterError(f"Model endpoint returned HTTP {exc.code}: {body}") from exc
            except transient_errors as exc:
                elapsed = time.perf_counter() - started
                if attempt >= self.max_retries:
                    logger.warning(
                        "Model request failed after %d attempt(s): request_id=%s url=%s elapsed=%.3fs error=%s",
                        attempt + 1,
                        request_id,
                        url,
                        elapsed,
                        exc,
                    )
                    raise ModelAdapterError(
                        f"Model endpoint connection failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                delay = self.retry_backoff_seconds * (2**attempt)
                logger.warning(
                    "Model request attempt failed; retrying: request_id=%s url=%s attempt=%d/%d elapsed=%.3fs delay=%.1fs error=%s",
                    request_id,
                    url,
                    attempt + 1,
                    self.max_retries + 1,
                    elapsed,
                    delay,
                    exc,
                )
                time.sleep(delay)

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("Model endpoint returned invalid JSON: request_id=%s url=%s", request_id, url)
            raise ModelAdapterError("Model endpoint returned invalid JSON.") from exc
        if not isinstance(decoded, Mapping):
            logger.warning("Model endpoint returned non-object JSON: request_id=%s url=%s", request_id, url)
            raise ModelAdapterError("Model endpoint returned a non-object JSON payload.")
        return decoded


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
