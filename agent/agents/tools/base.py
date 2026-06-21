"""Tool protocol, concrete function wrapper, and message parsing helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class ToolCall:
    """One function-call request emitted by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of executing one tool call, ready to send back to the model."""

    call_id: str
    content: str


class Tool(Protocol):
    """Protocol for tools usable by the formalizer."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]: ...

    def openai_schema(self) -> dict[str, Any]: ...

    def execute(self, arguments: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    """A concrete tool backed by a Python callable."""

    name: str
    description: str
    parameters: dict[str, Any]
    _execute: Callable[[dict[str, Any]], str]

    def execute(self, arguments: dict[str, Any]) -> str:
        return self._execute(arguments)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def extract_tool_calls(message: Mapping[str, Any]) -> tuple[ToolCall, ...]:
    """Extract OpenAI-style tool_calls from an assistant message."""
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return ()

    calls: list[ToolCall] = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        arguments_str = function.get("arguments", "{}")
        arguments: dict[str, Any] = {}
        if isinstance(arguments_str, str):
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                arguments = {"raw": arguments_str}
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)
