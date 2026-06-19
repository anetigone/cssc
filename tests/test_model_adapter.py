from __future__ import annotations

import os
import http.client
import unittest
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

from agent.search.action import ActionGenerationRequest
from agent.agents import (
    ChatActionGenerator,
    ChatConfig,
    ChatTransport,
    FunctionTool,
    ModelAdapterError,
)
from agent.agents.openai import UrllibChatTransport, chat_completions_url
from agent.proof_system.base import DiagnosticCategory, ParsedFeedback, ProofTask


class RecordingTransport(ChatTransport):
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, Any], float]] = []

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls.append((url, headers, payload, timeout_seconds))
        return self.response


class SequenceTransport(ChatTransport):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[Mapping[str, Any]] = []

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls.append(payload)
        return self.responses.pop(0)


class ChatActionGeneratorTests(unittest.TestCase):
    def test_generates_action_from_chat_completion_response(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "```lean\ntrivial\n```"},
                    }
                ]
            }
        )
        generator = ChatActionGenerator(
            ChatConfig(
                api_key="key",
                model="model",
                base_url="https://example.test/openai/v1/",
                timeout_seconds=12.0,
            ),
            transport=transport,
        )
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")

        actions = generator.generate(
            ActionGenerationRequest(
                task=task,
                attempt_index=1,
                previous_feedback=(
                    ParsedFeedback(
                        category=DiagnosticCategory.UNSOLVED_GOALS,
                        message="unsolved goals",
                    ),
                ),
                max_candidates=1,
            )
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].proof_text, "trivial")
        self.assertEqual(actions[0].action, "openai_chat")
        self.assertEqual(actions[0].metadata["model"], "model")
        url, headers, payload, timeout = transport.calls[0]
        self.assertEqual(url, "https://example.test/openai/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer key")
        self.assertEqual(payload["model"], "model")
        self.assertIn("unsolved goals", payload["messages"][1]["content"])
        self.assertEqual(timeout, 12.0)

    def test_repair_prompt_contains_previous_proof_and_relevant_checker_errors(self) -> None:
        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )
        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}")

        generator.generate(
            ActionGenerationRequest(
                task=task,
                attempt_index=1,
                metadata={
                    "previous_attempt": {
                        "proof_text": "exact badLemma",
                        "raw_output": (
                            "A.lean:1:1: information: noisy #check\n"
                            "A.lean:2:3: warning: noisy warning\n"
                            "A.lean:4:5: error: actual failure\n  detail"
                        ),
                    }
                },
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("exact badLemma", prompt)
        self.assertIn("error: actual failure\n  detail", prompt)
        self.assertNotIn("noisy #check", prompt)
        self.assertNotIn("noisy warning", prompt)

    def test_removes_exploration_commands_from_final_candidate(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": "#check True\nimport Mathlib\nclassical\n  exact True.intro"
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        actions = generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=0,
            )
        )

        self.assertEqual(actions[0].proof_text, "classical\n  exact True.intro")
        self.assertEqual(actions[0].metadata["removed_exploration_commands"], 2)

    def test_proof_generator_executes_environment_tool_calls(self) -> None:
        transport = SequenceTransport(
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
                                        "function": {"name": "lookup", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"content": "trivial"}, "finish_reason": "stop"}]},
                {"choices": [{"message": {"content": "trivial"}, "finish_reason": "stop"}]},
            ]
        )

        tool = FunctionTool(
            name="lookup",
            description="Look up Lean names.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda _: '{"found": true}',
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"),
            transport=transport,
            tools=[tool],
        )

        actions = generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=0,
            )
        )

        self.assertEqual(actions[0].proof_text, "trivial")
        tool_messages = [m for m in transport.calls[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")

    def test_type_mismatch_repair_disables_tools(self) -> None:
        transport = RecordingTransport(
            {"choices": [{"message": {"content": "exact fixed"}, "finish_reason": "stop"}]}
        )
        tool = FunctionTool(
            name="check_lean_snippet",
            description="Check Lean.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda _: '{"ok": true}',
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"),
            transport=transport,
            tools=[tool],
        )

        actions = generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                previous_feedback=(
                    ParsedFeedback(
                        category=DiagnosticCategory.TYPE_MISMATCH,
                        message="Type mismatch",
                    ),
                ),
            )
        )

        self.assertEqual(actions[0].proof_text, "exact fixed")
        payload = transport.calls[0][2]
        self.assertNotIn("tools", payload)
        self.assertNotIn("check_lean_snippet", payload["messages"][0]["content"])

    def test_from_env_requires_key_and_model(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelAdapterError):
                ChatConfig.from_env(timeout_seconds=60.0)

    def test_chat_completions_url_accepts_full_endpoint(self) -> None:
        self.assertEqual(
            chat_completions_url("https://example.test/v1/chat/completions"),
            "https://example.test/v1/chat/completions",
        )

    def test_transport_retries_remote_disconnect(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices": []}'
        transport = UrllibChatTransport(max_retries=2, retry_backoff_seconds=0)

        with patch(
            "agent.agents.openai.urllib.request.urlopen",
            side_effect=[http.client.RemoteDisconnected("closed"), response],
        ) as urlopen:
            result = transport.post_json(
                "https://example.test/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                payload={"model": "m"},
                timeout_seconds=10,
            )

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 2)

    def test_transport_logs_request_start_and_completion(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices": []}'
        response.__enter__.return_value.status = 200
        transport = UrllibChatTransport(max_retries=0)

        with (
            patch("agent.agents.openai.urllib.request.urlopen", return_value=response),
            patch("agent.agents.openai.uuid.uuid4") as uuid4,
            self.assertLogs("agent.agents.openai", level="DEBUG") as logs,
        ):
            uuid4.return_value.hex = "12345678abcdef"
            transport.post_json(
                "https://example.test/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                payload={"model": "m"},
                timeout_seconds=10,
            )

        output = "\n".join(logs.output)
        self.assertIn("Model request started: request_id=12345678", output)
        self.assertIn("Model request completed: request_id=12345678", output)
        self.assertIn("status=200", output)
        self.assertIn("elapsed=", output)

    def test_transport_wraps_remote_disconnect_after_retries(self) -> None:
        transport = UrllibChatTransport(max_retries=1, retry_backoff_seconds=0)

        with patch(
            "agent.agents.openai.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaisesRegex(ModelAdapterError, "after 2 attempt"):
                transport.post_json(
                    "https://example.test/v1/chat/completions",
                    headers={"Content-Type": "application/json"},
                    payload={"model": "m"},
                    timeout_seconds=10,
                )


if __name__ == "__main__":
    unittest.main()
