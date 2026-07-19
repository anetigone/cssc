from __future__ import annotations

import _thread
import json
import threading
import time
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
    def test_blocking_transport_remains_keyboard_interruptible(self) -> None:
        release = threading.Event()

        class BlockingTransport:
            def post_json(
                self,
                url: str,
                headers: Mapping[str, str],
                payload: Mapping[str, Any],
                timeout_seconds: float,
            ) -> Mapping[str, Any]:
                release.wait(5)
                return {"choices": [{"message": {"content": "too late"}}]}

        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=BlockingTransport(),
            tools=(),
        )
        timer = threading.Timer(0.05, _thread.interrupt_main)
        started = time.perf_counter()
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                driver.complete([{"role": "user", "content": "hi"}])
        finally:
            release.set()
            timer.cancel()

        self.assertLess(time.perf_counter() - started, 1.0)

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

    def test_complete_aggregates_visible_usage_across_tool_loop(self) -> None:
        tool = FunctionTool(
            name="echo",
            description="Echo.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda args: json.dumps(args),
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
                                        "function": {
                                            "name": "echo",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 6,
                        "completion_tokens_details": {"reasoning_tokens": 4},
                    },
                },
                {
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 3},
                },
            ]
        )
        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=transport,
            tools=[tool],
            max_tool_rounds=2,
        )

        response = driver.complete([{"role": "user", "content": "hi"}])
        usage = response["_agent_token_usage"]

        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["reasoning_tokens"], 4)

    def test_complete_reuses_tool_capable_response_when_it_is_already_final(self) -> None:
        tool = FunctionTool(
            name="echo",
            description="Echo.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda args: json.dumps(args),
        )
        transport = RecordingTransport(
            [{"choices": [{"message": {"role": "assistant", "content": "done"}}]}]
        )
        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=transport,
            tools=[tool],
        )

        response = driver.complete([{"role": "user", "content": "hi"}], final_n=1)

        self.assertEqual(response["choices"][0]["message"]["content"], "done")
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("tools", transport.calls[0])

    def test_tool_budget_forces_tool_free_final_and_skips_duplicates(self) -> None:
        executions: list[dict[str, object]] = []
        tool = FunctionTool(
            name="echo",
            description="Echo.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda args: executions.append(args) or json.dumps({"ok": True}),
        )
        tool_response = lambda call_id: {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": "echo", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
        transport = RecordingTransport(
            [
                tool_response("call_1"),
                tool_response("call_2"),
                {"choices": [{"message": {"content": "final proof"}}]},
            ]
        )
        driver = ChatDriver(
            ChatConfig(api_key="k", model="m"),
            transport=transport,
            tools=[tool],
            max_tool_rounds=2,
        )

        response = driver.complete([{"role": "user", "content": "prove it"}])

        self.assertEqual(response["choices"][0]["message"]["content"], "final proof")
        self.assertEqual(len(executions), 1)
        self.assertEqual(len(transport.calls), 3)
        self.assertIn("tools", transport.calls[0])
        self.assertIn("tools", transport.calls[1])
        self.assertNotIn("tools", transport.calls[2])
        self.assertIn(
            "tool budget is exhausted",
            transport.calls[2]["messages"][-1]["content"].lower(),
        )

    def test_complete_keeps_final_request_for_multiple_candidates(self) -> None:
        tool = FunctionTool(
            name="echo",
            description="Echo.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda args: json.dumps(args),
        )
        transport = RecordingTransport(
            [
                {"choices": [{"message": {"content": "single"}}]},
                {"choices": [{"message": {"content": "many"}}]},
            ]
        )
        driver = ChatDriver(ChatConfig(api_key="k", model="m"), transport=transport, tools=[tool])

        response = driver.complete([], final_n=3)

        self.assertEqual(response["choices"][0]["message"]["content"], "many")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]["n"], 3)
        self.assertNotIn("tools", transport.calls[1])

    def test_execute_tool_returns_error_for_unknown_tool(self) -> None:
        driver = ChatDriver(ChatConfig(api_key="k", model="m"), transport=RecordingTransport([]))
        result = driver.execute_tool(ToolCall(id="c1", name="missing", arguments={}))
        self.assertEqual(json.loads(result.content)["error"], "Unknown tool: missing")

    def test_first_choice_message_raises_on_missing_choices(self) -> None:
        with self.assertRaises(ModelAdapterError):
            first_choice_message({})


if __name__ == "__main__":
    unittest.main()
