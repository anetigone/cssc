from __future__ import annotations

import unittest
from typing import Any, Mapping

from agent.agents.context import (
    ChatContextSummarizer,
    SummarizationRequest,
    SummarizationResult,
)
from agent.agents.openai import ChatConfig
from agent.proof_system.base import DiagnosticCategory, ParsedFeedback, ProofTask


class RecordingTransport:
    """Capture the payload sent to the chat endpoint."""

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


class ChatContextSummarizerTests(unittest.TestCase):
    def test_skips_summarization_on_first_attempt(self) -> None:
        transport = RecordingTransport({"choices": []})
        summarizer = ChatContextSummarizer(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        result = summarizer.summarize(
            SummarizationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=0,
            )
        )

        self.assertFalse(result.was_summarized)
        self.assertEqual(transport.calls, [])

    def test_parses_json_summary(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"concise_error": "type mismatch at exact", '
                                '"relevant_history": ["unknown identifier x"], '
                                '"retained_retrieved": ["Nat.add_comm"], '
                                '"strategy_hint": "use exact? instead"}\n'
                                "```"
                            ),
                        },
                        "finish_reason": "stop",
                    },
                ]
            }
        )
        summarizer = ChatContextSummarizer(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        result = summarizer.summarize(
            SummarizationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                previous_attempt={
                    "proof_text": "exact x",
                    "category": "type_mismatch",
                    "raw_output": "sample.lean:2:2: error: type mismatch",
                },
                feedback_history=(
                    ParsedFeedback(
                        category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
                        message="unknown identifier x",
                    ),
                ),
            )
        )

        self.assertTrue(result.was_summarized)
        self.assertEqual(result.concise_error, "type mismatch at exact")
        self.assertEqual(result.relevant_history, ("unknown identifier x",))
        self.assertEqual(result.retained_retrieved, ("Nat.add_comm",))
        self.assertEqual(result.strategy_hint, "use exact? instead")
        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("Previous proof body:", prompt)
        self.assertIn("Raw checker output:", prompt)

    def test_falls_back_to_plain_text_on_non_json_response(self) -> None:
        transport = RecordingTransport(
            {"choices": [{"message": {"content": "the proof failed because x is unknown"}}]}
        )
        summarizer = ChatContextSummarizer(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        result = summarizer.summarize(
            SummarizationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                previous_attempt={
                    "proof_text": "exact x",
                    "raw_output": "error: unknown identifier x",
                },
            )
        )

        self.assertTrue(result.was_summarized)
        self.assertEqual(result.concise_error, "the proof failed because x is unknown")
        self.assertEqual(result.strategy_hint, "")

    def test_includes_feedback_history_in_prompt(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"concise_error": "fail", "strategy_hint": "hint"}'
                        }
                    }
                ]
            }
        )
        summarizer = ChatContextSummarizer(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        summarizer.summarize(
            SummarizationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=2,
                previous_attempt={
                    "proof_text": "exact x",
                    "raw_output": "error",
                },
                feedback_history=(
                    ParsedFeedback(
                        category=DiagnosticCategory.TYPE_MISMATCH,
                        message="type mismatch",
                        unsolved_goals=("goal 1", "goal 2"),
                    ),
                ),
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("type mismatch", prompt)
        self.assertIn("unsolved goals:", prompt)

    def test_uses_config_max_tokens_in_payload(self) -> None:
        transport = RecordingTransport(
            {"choices": [{"message": {"content": '{"concise_error": "err"}'}}]}
        )
        summarizer = ChatContextSummarizer(
            ChatConfig(api_key="key", model="model", max_tokens=256),
            transport=transport,
        )

        summarizer.summarize(
            SummarizationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                previous_attempt={
                    "proof_text": "exact x",
                    "raw_output": "error",
                },
            )
        )

        payload = transport.calls[0][2]
        self.assertEqual(payload["max_tokens"], 256)


if __name__ == "__main__":
    unittest.main()
