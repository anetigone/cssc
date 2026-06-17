from __future__ import annotations

import unittest
from typing import Any, Mapping

from agent.agents import ChatTransport, FormalizationRequest, OpenAIChatConfig, OpenAIChatFormalizationAgent


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


if __name__ == "__main__":
    unittest.main()
