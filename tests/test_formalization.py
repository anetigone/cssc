from __future__ import annotations

import unittest
from typing import Any, Mapping

from agent.agents import ChatTransport, FormalizationRequest, OpenAIChatConfig, OpenAIChatFormalizationAgent
from agent.input.validation import ScaffoldValidationResult, ValidationConfig


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


class FakeChecker:
    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._index = 0

    def validate_scaffold(self, source: str, *, imports: tuple[str, ...] = (), **kwargs: object) -> ScaffoldValidationResult:
        self.calls.append((source, kwargs))
        ok = self.results[self._index]
        self._index += 1
        if ok:
            return ScaffoldValidationResult(ok=True, message="ok")
        return ScaffoldValidationResult(ok=False, message="syntax error")


class SequenceTransport(ChatTransport):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, Any], float]] = []
        self._index = 0

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.calls.append((url, headers, payload, timeout_seconds))
        response = self.responses[self._index]
        self._index += 1
        return response


class FormalizationAgentTests(unittest.TestCase):
    def test_openai_formalizer_parses_json_scaffold(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"proof_source":"theorem sample : True := by\\n  sorry",'
                                '"natural_language_proof":"True is immediate."}'
                            )
                        }
                    }
                ]
            }
        )
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(
                api_key="key",
                model="formalizer-model",
                base_url="https://example.test/v1",
                timeout_seconds=7.0,
            ),
            transport=transport,
        )

        result = formalizer.formalize(
            FormalizationRequest(problem="Prove True.", task_id="sample")
        )

        self.assertEqual(result.proof_source, "theorem sample : True := by\n  sorry")
        self.assertEqual(result.natural_language_proof, "True is immediate.")
        url, headers, payload, timeout = transport.calls[0]
        self.assertEqual(url, "https://example.test/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer key")
        self.assertEqual(payload["model"], "formalizer-model")
        self.assertIn("Prove True.", payload["messages"][1]["content"])
        self.assertEqual(timeout, 7.0)

    def test_openai_formalizer_validates_json_shape(self) -> None:
        from agent.input.validation import ScaffoldValidationError

        transport = RecordingTransport(
            {
                "choices": [
                    {"message": {"content": '{"natural_language_proof":"missing proof_source"}'}}
                ]
            }
        )
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
            transport=transport,
        )

        with self.assertRaises(ScaffoldValidationError):
            formalizer.formalize(FormalizationRequest(problem="Prove True."))

    def test_openai_formalizer_retries_on_lean_check_failure(self) -> None:
        transport = SequenceTransport(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"proof_source":"theorem sample : True := by\\n  sorry"}'
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"proof_source":"theorem sample : True := by\\n  trivial"}'
                            }
                        }
                    ]
                },
            ]
        )
        checker = FakeChecker([False, True])
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
            transport=transport,
            checker=checker,
            validation=ValidationConfig(max_retries=1),
        )

        result = formalizer.formalize(FormalizationRequest(problem="Prove True."))

        self.assertEqual(result.proof_source, "theorem sample : True := by\n  trivial")
        self.assertEqual(len(transport.calls), 2)
        retry_prompt = transport.calls[1][2]["messages"][2]["content"]
        self.assertIn("syntax error", retry_prompt)

    def test_openai_formalizer_raises_after_max_retries(self) -> None:
        from agent.agents import ModelAdapterError

        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"proof_source":"theorem sample : True := by\\n  sorry"}'
                        }
                    }
                ]
            }
        )
        checker = FakeChecker([False])
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
            transport=transport,
            checker=checker,
            validation=ValidationConfig(max_retries=0),
        )

        with self.assertRaises(ModelAdapterError):
            formalizer.formalize(FormalizationRequest(problem="Prove True."))

        self.assertEqual(len(transport.calls), 1)

    def test_openai_formalizer_skips_check_when_checker_none(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"proof_source":"theorem sample : True := by\\n  sorry"}'
                        }
                    }
                ]
            }
        )
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
            transport=transport,
            checker=None,
        )

        result = formalizer.formalize(FormalizationRequest(problem="Prove True."))

        self.assertEqual(result.proof_source, "theorem sample : True := by\n  sorry")
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
