from __future__ import annotations

import os
import http.client
import unittest
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

from agent.search.action import ActionGenerationRequest
from agent.agents import (
    ChatTransport,
    ModelAdapterError,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
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


class OpenAIChatActionGeneratorTests(unittest.TestCase):
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
        generator = OpenAIChatActionGenerator(
            OpenAIChatConfig(
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

    def test_from_env_requires_key_and_model(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelAdapterError):
                OpenAIChatConfig.from_env(timeout_seconds=60.0)

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
