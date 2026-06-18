from __future__ import annotations

import json
import unittest
from typing import Any, Mapping

from agent.agents import (
    ChatConfig,
    ChatDriver,
    FunctionTool,
    ModelAdapterError,
    ToolCall,
)
from agent.agents.chat_driver import first_choice_message


class RecordingTransport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[Mapping[str, Any]] = []
        self._index = 0

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls.append(payload)
        response = self.responses[self._index]
        self._index += 1
        return response


class ChatDriverTests(unittest.TestCase):
    def test_complete_returns_final_response(self) -> None:
        transport = RecordingTransport(
            [{"choices": [{"message": {"content": "hello"}}]}]
        )
        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=transport,
            tools=(),
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]

        response = driver.complete(messages, final_n=1)

        self.assertEqual(response["choices"][0]["message"]["content"], "hello")
        self.assertEqual(transport.calls[0]["n"], 1)

    def test_complete_uses_final_n_for_final_request(self) -> None:
        transport = RecordingTransport(
            [{"choices": [{"message": {"content": "answer"}}]}]
        )
        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=transport,
            tools=(),
        )

        driver.complete([], final_n=3)

        self.assertEqual(transport.calls[0]["n"], 3)

    def test_complete_runs_tool_calls_before_final_answer(self) -> None:
        calls: list[ToolCall] = []

        def echo(args: dict[str, object]) -> str:
            calls.append(ToolCall(id="recorded", name="echo", arguments=args))
            return json.dumps({"ok": True})

        tool = FunctionTool(
            name="echo",
            description="Echo.",
            parameters={"type": "object", "properties": {}},
            _execute=echo,
        )
        transport = RecordingTransport(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "echo", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"content": None}}]},
                {"choices": [{"message": {"content": "done"}}]},
            ]
        )
        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=transport,
            tools=[tool],
            max_tool_rounds=3,
        )

        response = driver.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(response["choices"][0]["message"]["content"], "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(transport.calls), 3)
        tool_messages = [m for m in transport.calls[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
        self.assertNotIn("tools", transport.calls[2])

    def test_execute_tool_returns_error_for_unknown_tool(self) -> None:
        driver = ChatDriver(ChatConfig(api_key="k", model="m"), transport=RecordingTransport([]))
        result = driver.execute_tool(ToolCall(id="c1", name="missing", arguments={}))
        self.assertEqual(json.loads(result.content)["error"], "Unknown tool: missing")

    def test_first_choice_message_raises_on_missing_choices(self) -> None:
        with self.assertRaises(ModelAdapterError):
            first_choice_message({})


if __name__ == "__main__":
    unittest.main()
