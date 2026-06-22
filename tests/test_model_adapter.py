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

    def test_retry_prompt_contains_previous_proof_and_relevant_checker_errors(self) -> None:
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
                    "proof_phase": "retry",
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
        system_prompt = transport.calls[0][2]["messages"][0]["content"]
        self.assertIn("previous attempt failed", system_prompt)
        self.assertIn("smallest change", system_prompt)

    def test_restart_prompt_allows_strategy_reconsideration(self) -> None:
        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=2,
                metadata={"proof_phase": "retry"},
            )
        )

        system_prompt = transport.calls[0][2]["messages"][0]["content"]
        self.assertIn("Reconsider the failing", system_prompt)

    def test_repair_phase_treated_as_revision(self) -> None:
        # Structured mode emits proof_phase="repair" on subsequent attempts;
        # it must reach the same revision guidance as minimal's "retry",
        # otherwise a repair request is silently treated as a fresh propose.
        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )
        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                metadata={"proof_phase": "repair"},
            )
        )
        system_prompt = transport.calls[0][2]["messages"][0]["content"]
        self.assertIn("previous attempt failed", system_prompt)

    def test_structured_projection_renders_in_prompt(self) -> None:
        transport = RecordingTransport(
            {"choices": [{"message": {"content": "fixed"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )
        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}")
        generator.generate(
            ActionGenerationRequest(
                task=task,
                attempt_index=2,
                metadata={
                    "proof_phase": "repair",
                    "branch_obligation": {
                        "obligation_id": "sample",
                        "lean_statement": "theorem sample : True := by",
                        "statement_nl": "Show the sample theorem holds.",
                    },
                    "previous_attempt": {
                        "branch_id": "sample:root",
                        "proof_text": "exact trivial",
                        "observations": [
                            {
                                "category": "unsolved_goals",
                                "message": "unsolved goals",
                                "goal_fingerprint": "fp1",
                            }
                        ],
                    },
                    "verified_facts": (
                        {
                            "obligation_id": "helper",
                            "statement": "lemma helper : True := rfl",
                        },
                    ),
                },
            )
        )
        prompt = transport.calls[0][2]["messages"][1]["content"]
        # The branch's obligation anchors the proposal to the right goal.
        self.assertIn("Show the sample theorem holds.", prompt)
        self.assertIn("theorem sample : True := by", prompt)
        # The failed realization from the retained artifact must be revisable.
        self.assertIn("exact trivial", prompt)
        # Accepted facts from other branches are surfaced for reuse.
        self.assertIn("lemma helper : True := rfl", prompt)

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

    def test_prefers_summarized_context_over_raw_output(self) -> None:
        from agent.agents.context import SummarizationResult

        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                metadata={
                    "proof_phase": "retry",
                    "previous_attempt": {
                        "proof_text": "exact badLemma",
                        "raw_output": "A.lean:4:5: error: very long diagnostic\n" * 50,
                    },
                    "summarized_context": SummarizationResult(
                        concise_error="unknown lemma badLemma",
                        strategy_hint="use a lemma from Mathlib",
                        was_summarized=True,
                    ),
                },
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("unknown lemma badLemma", prompt)
        self.assertIn("use a lemma from Mathlib", prompt)
        self.assertNotIn("very long diagnostic", prompt)

    def test_summarized_relevant_history_appears_in_prompt(self) -> None:
        from agent.agents.context import SummarizationResult

        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                metadata={
                    "proof_phase": "retry",
                    "previous_attempt": {"proof_text": "exact badLemma"},
                    "summarized_context": SummarizationResult(
                        concise_error="unknown lemma",
                        relevant_history=("unknown identifier badLemma",),
                        was_summarized=True,
                    ),
                },
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("Key history from prior attempts:", prompt)
        self.assertIn("- unknown identifier badLemma", prompt)

    def test_retained_retrieved_filters_snippets(self) -> None:
        from agent.agents.context import SummarizationResult
        from agent.retrieval import RetrievalResult

        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )

        kept = RetrievalResult(
            name="Nat.add_comm",
            source_path=None,
            start_line=1,
            snippet="theorem Nat.add_comm ...",
            score=0.9,
        )
        dropped = RetrievalResult(
            name="Nat.mul_comm",
            source_path=None,
            start_line=1,
            snippet="theorem Nat.mul_comm ...",
            score=0.4,
        )
        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                metadata={
                    "proof_phase": "retry",
                    "previous_attempt": {"proof_text": "exact badLemma"},
                    "retrieved_results": (kept, dropped),
                    "summarized_context": SummarizationResult(
                        retained_retrieved=("Nat.add_comm",),
                        was_summarized=True,
                    ),
                },
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("Nat.add_comm", prompt)
        self.assertIn("theorem Nat.add_comm ...", prompt)
        self.assertNotIn("Nat.mul_comm", prompt)

    def test_empty_retained_retrieved_drops_all_snippets(self) -> None:
        from agent.agents.context import SummarizationResult
        from agent.retrieval import RetrievalResult

        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )
        retrieved = RetrievalResult(
            name="Nat.add_comm",
            source_path=None,
            start_line=1,
            snippet="theorem Nat.add_comm ...",
            score=0.9,
        )

        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=1,
                metadata={
                    "proof_phase": "retry",
                    "previous_attempt": {
                        "proof_text": "exact badLemma",
                        "raw_output": "unknown identifier badLemma",
                    },
                    "retrieved_results": (retrieved,),
                    "summarized_context": SummarizationResult(
                        concise_error="unknown identifier",
                        retained_retrieved=(),
                        was_summarized=True,
                    ),
                },
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertNotIn("Retrieved Lean snippets:", prompt)
        self.assertNotIn("Nat.add_comm", prompt)

    def test_no_summary_keeps_all_retrieved_snippets(self) -> None:
        from agent.retrieval import RetrievalResult

        transport = RecordingTransport(
            {"choices": [{"message": {"content": "corrected"}, "finish_reason": "stop"}]}
        )
        generator = ChatActionGenerator(
            ChatConfig(api_key="key", model="model"), transport=transport
        )
        retrieved = (
            RetrievalResult(
                name="Nat.add_comm",
                source_path=None,
                start_line=1,
                snippet="theorem Nat.add_comm ...",
                score=0.9,
            ),
        )

        # No summarized_context at all (first attempt or summarizer disabled).
        generator.generate(
            ActionGenerationRequest(
                task=ProofTask("sample", "theorem sample : True := by\n  {{proof}}"),
                attempt_index=0,
                metadata={"retrieved_results": retrieved},
            )
        )

        prompt = transport.calls[0][2]["messages"][1]["content"]
        self.assertIn("Retrieved Lean snippets:", prompt)
        self.assertIn("Nat.add_comm", prompt)

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
