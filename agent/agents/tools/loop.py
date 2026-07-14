"""Tool-call loop driver shared by formalizer and proof agents."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Mapping, Sequence

from ..openai import (
    ChatConfig,
    ChatTransport,
    ModelAdapterError,
    chat_completions_url,
    merge_token_usage,
    normalized_token_usage,
)
from .base import Tool, ToolCall, ToolResult, extract_tool_calls


logger = logging.getLogger(__name__)
AGENT_TOKEN_USAGE_KEY = "_agent_token_usage"
AGENT_TOOL_CALLS_KEY = "_agent_tool_calls"
AGENT_PROVIDER_REQUESTS_KEY = "_agent_provider_requests"


def run_tool_loop(
    transport: ChatTransport,
    config: ChatConfig,
    messages: list[dict[str, Any]],
    tools: Sequence[Tool],
    max_rounds: int,
    execute_tool: Callable[[ToolCall], ToolResult],
    *,
    base_payload: Mapping[str, Any],
    final_n: int = 1,
) -> Mapping[str, Any]:
    """Run a chat completion, allowing the model to call tools first.

    Tool-call rounds use ``n=1`` so that the single stream of tool messages is
    well defined. A tool-capable response that already contains a final answer
    is returned directly when ``final_n == 1``. A separate tool-free request is
    only needed for multiple candidates, when the tool-capable response has no
    usable content, or when the tool budget is exhausted. At the budget limit,
    tools are removed and the model is forced to provide a final answer.
    """
    provider_requests: list[dict[str, object]] = []
    request_usages: list[dict[str, int]] = []
    tool_events: list[dict[str, object]] = []

    def post(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        started = time.perf_counter()
        try:
            response = transport.post_json(
                chat_completions_url(config.base_url),
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=config.timeout_seconds,
            )
        except ModelAdapterError as exc:
            traced = exc.metadata.get("provider_requests")
            if isinstance(traced, (list, tuple)):
                provider_requests.extend(
                    dict(item) for item in traced if isinstance(item, Mapping)
                )
            else:
                provider_requests.append({
                    "request_id": f"request:{len(provider_requests)}",
                    "retry_index": 0,
                    "status": "failed",
                    "wall_time_ms": (time.perf_counter() - started) * 1000,
                    "error": type(exc).__name__,
                })
            raise ModelAdapterError(
                str(exc),
                metadata={
                    **exc.metadata,
                    "provider_requests": tuple(provider_requests),
                    "tool_calls": tuple(tool_events),
                    "token_usage": merge_token_usage(*request_usages),
                },
            ) from exc
        drain = getattr(transport, "drain_provider_request_events", None)
        traced = drain() if callable(drain) else None
        if isinstance(traced, (list, tuple)):
            provider_requests.extend(
                dict(item) for item in traced if isinstance(item, Mapping)
            )
        else:
            provider_requests.append({
                "request_id": f"request:{len(provider_requests)}",
                "retry_index": 0,
                "status": "completed",
                "wall_time_ms": (time.perf_counter() - started) * 1000,
                "token_usage": normalized_token_usage(response),
            })
        return response

    if not tools:
        payload = dict(base_payload)
        payload["n"] = final_n
        response = post(payload)
        return _with_usage(
            response, (normalized_token_usage(response),),
            provider_requests=provider_requests,
        )

    tool_rounds = 0
    seen_tool_calls: set[tuple[str, str]] = set()
    while tool_rounds < max_rounds:
        payload = dict(base_payload)
        payload["n"] = 1
        payload["tools"] = [tool.openai_schema() for tool in tools]
        payload["tool_choice"] = "auto"
        response = post(payload)
        request_usages.append(normalized_token_usage(response))
        message = _first_message(response)
        calls = extract_tool_calls(message)
        if not calls:
            content = message.get("content")
            if final_n == 1 and isinstance(content, str) and content.strip():
                return _with_usage(
                    response, request_usages, tool_events, provider_requests
                )
            break
        tool_rounds += 1
        logger.info(
            "Executing model tool calls: round=%d/%d calls=%d",
            tool_rounds,
            max_rounds,
            len(calls),
        )
        messages.append(dict(message))
        for call in calls:
            call_key = (
                call.name,
                json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str),
            )
            if call_key in seen_tool_calls:
                logger.warning(
                    "Skipping duplicate model tool call: round=%d/%d tool=%s",
                    tool_rounds,
                    max_rounds,
                    call.name,
                )
                result = ToolResult(
                    call_id=call.id,
                    content=json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "Duplicate tool call skipped. Use the previous result and "
                                "produce the final answer."
                            ),
                        }
                    ),
                )
                tool_events.append({
                    "call_id": call.id,
                    "tool_kind": call.name,
                    "status": "skipped_duplicate",
                    "wall_time_ms": 0.0,
                })
            else:
                seen_tool_calls.add(call_key)
                started = time.perf_counter()
                try:
                    result = execute_tool(call)
                except Exception as exc:
                    tool_events.append({
                        "call_id": call.id,
                        "tool_kind": call.name,
                        "status": "failed",
                        "wall_time_ms": (time.perf_counter() - started) * 1000,
                        "error": type(exc).__name__,
                    })
                    raise
                tool_events.append({
                    "call_id": result.call_id,
                    "tool_kind": call.name,
                    "status": "completed",
                    "wall_time_ms": (time.perf_counter() - started) * 1000,
                })
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )

    if tool_rounds >= max_rounds:
        logger.info(
            "Tool-call budget exhausted; requesting tool-free final answer: rounds=%d",
            tool_rounds,
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "The Lean tool budget is exhausted. Do not call tools again. Return only "
                    "the final proof body that replaces the proof marker, with no markdown, "
                    "imports, #check, #print, #eval, or #reduce commands."
                ),
            }
        )

    final_payload = dict(base_payload)
    final_payload["n"] = final_n
    response = post(final_payload)
    request_usages.append(normalized_token_usage(response))
    return _with_usage(response, request_usages, tool_events, provider_requests)


def _with_usage(
    response: Mapping[str, Any],
    request_usages: Sequence[Mapping[str, Any]],
    tool_calls: Sequence[Mapping[str, object]] = (),
    provider_requests: Sequence[Mapping[str, object]] = (),
) -> Mapping[str, Any]:
    enriched = dict(response)
    enriched[AGENT_TOKEN_USAGE_KEY] = merge_token_usage(*request_usages)
    enriched[AGENT_TOOL_CALLS_KEY] = tuple(dict(call) for call in tool_calls)
    enriched[AGENT_PROVIDER_REQUESTS_KEY] = tuple(
        dict(request) for request in provider_requests
    )
    return enriched


def _first_message(response: Mapping[str, Any]) -> dict[str, Any]:
    """Return the first assistant message from a chat-completions response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelAdapterError("Model response is missing a choices list.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ModelAdapterError("Model choice is not an object.")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ModelAdapterError("Model choice is missing a message.")
    return dict(message)
