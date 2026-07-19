"""Shared chat-completion driver for agent roles.

The driver wraps an OpenAI-compatible chat endpoint and handles the
boilerplate that every agent repeats: payload construction, tool-call loops,
and response extraction. Individual agents only need to supply messages and
parse the final content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .openai import ChatConfig, ChatTransport, ModelAdapterError
from .tools import Tool, ToolCall, ToolResult, run_tool_loop


logger = logging.getLogger(__name__)


@dataclass
class ChatDriver:
    """Drive a chat completion, optionally letting the model call tools first.

    This is intentionally thin: it does not own prompt construction or output
    parsing, so it can be reused by the formalizer, proof generator, and any
    future agent that speaks the same chat-completion protocol.
    """

    config: ChatConfig
    transport: ChatTransport
    tools: Sequence[Tool] = field(default_factory=tuple)
    max_tool_rounds: int = 5

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        final_n: int = 1,
        allow_tools: bool = True,
        tool_budget_final_instruction: str | None = None,
    ) -> Mapping[str, Any]:
        """Run a chat completion and return the decoded response.

        When ``tools`` is non-empty, the model is allowed to call tools for up
        to ``max_tool_rounds`` rounds before the final answer. The final request
        uses ``n=final_n`` so callers can request multiple candidate answers.
        """
        base_payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            **self.config.extra_body,
        }
        active_tools = self.tools if allow_tools else ()
        return run_tool_loop(
            self.transport,
            self.config,
            messages,
            active_tools,
            self.max_tool_rounds,
            self.execute_tool,
            base_payload=base_payload,
            final_n=final_n,
            tool_budget_final_instruction=tool_budget_final_instruction,
        )

    def execute_tool(self, call: ToolCall) -> ToolResult:
        """Dispatch a single tool call to the matching registered tool."""
        for tool in self.tools:
            if tool.name == call.name:
                return ToolResult(
                    call_id=call.id,
                    content=tool.execute(call.arguments),
                )
        return ToolResult(
            call_id=call.id,
            content=json.dumps({"error": f"Unknown tool: {call.name}"}),
        )


def first_choice_message(response: Mapping[str, Any]) -> dict[str, Any]:
    """Return the first assistant message from a chat-completions response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelAdapterError("Model response is missing choices.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ModelAdapterError("Model choice is not an object.")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ModelAdapterError("Model choice is missing a message.")
    return dict(message)
