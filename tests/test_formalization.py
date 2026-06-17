from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from typing import Any, Mapping

from agent.agents import (
    ChatTransport,
    FormalizationRequest,
    FormalizationResult,
    OpenAIChatConfig,
    OpenAIChatFormalizationAgent,
    VerifiedFormalizationCache,
)
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

    def test_openai_formalizer_prompt_discourages_full_mathlib_import(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {"message": {"content": '{"proof_source":"theorem sample : True := by\\n  sorry"}'}}
                ]
            }
        )
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
            transport=transport,
        )

        formalizer.formalize(FormalizationRequest(problem="Prove True."))

        system_prompt = transport.calls[0][2]["messages"][0]["content"]
        self.assertIn("smallest Lean imports", system_prompt)
        self.assertIn("Do not use `import Mathlib`", system_prompt)
        self.assertIn("Mathlib.Topology.Algebra.Ring.Real", system_prompt)
        self.assertIn("Mathlib.Topology.Order.Real", system_prompt)
        self.assertIn("Do not import `Mathlib.Topology.Instances.Real`", system_prompt)

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

    def test_openai_formalizer_normalizes_known_bad_real_topology_import(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"proof_source":"import Mathlib.Data.Real.Basic\\n'
                                'import Mathlib.Topology.Instances.Real\\n\\n'
                                'theorem sample {E : Set ℝ} : sSup E = sSup E := by\\n  sorry"}'
                            )
                        }
                    }
                ]
            }
        )
        checker = FakeChecker([True])
        formalizer = OpenAIChatFormalizationAgent(
            OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
            transport=transport,
            checker=checker,
        )

        result = formalizer.formalize(FormalizationRequest(problem="Prove a real supremum fact."))

        self.assertIn("import Mathlib.Topology.Algebra.Ring.Real", result.proof_source)
        self.assertIn("import Mathlib.Topology.Order.Real", result.proof_source)
        self.assertNotIn("import Mathlib.Topology.Instances.Real", result.proof_source)

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

    def test_openai_formalizer_reads_validated_cache_before_model_call(self) -> None:
        transport = RecordingTransport(
            {
                "choices": [
                    {"message": {"content": '{"proof_source":"theorem sample : True := by\\n  sorry"}'}}
                ]
            }
        )
        checker = FakeChecker([True])
        request = FormalizationRequest(problem="Prove True.", task_id="sample")
        with tempfile.TemporaryDirectory() as tmp:
            cache = VerifiedFormalizationCache(Path(tmp))
            cache.put(
                request,
                FormalizationResult(
                    "theorem cached : True := by\n  sorry",
                    "Cached proof.",
                    metadata={"model": "m"},
                ),
                model="m",
            )
            formalizer = OpenAIChatFormalizationAgent(
                OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
                transport=transport,
                checker=checker,
                cache=cache,
            )

            result = formalizer.formalize(request)

        self.assertEqual(result.proof_source, "theorem cached : True := by\n  sorry")
        self.assertEqual(result.natural_language_proof, "Cached proof.")
        self.assertTrue(result.metadata["formalization_cache_hit"])
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(len(checker.calls), 0)

    def test_openai_formalizer_writes_cache_only_after_validation_success(self) -> None:
        transport = SequenceTransport(
            [
                {
                    "choices": [
                        {"message": {"content": '{"proof_source":"theorem bad : True := by\\n  sorry"}'}}
                    ]
                },
                {
                    "choices": [
                        {"message": {"content": '{"proof_source":"theorem good : True := by\\n  trivial"}'}}
                    ]
                },
            ]
        )
        checker = FakeChecker([False, True])
        request = FormalizationRequest(problem="Prove True.", task_id="sample")
        with tempfile.TemporaryDirectory() as tmp:
            cache = VerifiedFormalizationCache(Path(tmp))
            formalizer = OpenAIChatFormalizationAgent(
                OpenAIChatConfig(api_key="key", model="m", base_url="https://example.test/v1"),
                transport=transport,
                checker=checker,
                validation=ValidationConfig(max_retries=1),
                cache=cache,
            )

            result = formalizer.formalize(request)
            cached_files = list(Path(tmp).glob("*.json"))
            cached_text = cached_files[0].read_text(encoding="utf-8")

        self.assertEqual(result.proof_source, "theorem good : True := by\n  trivial")
        self.assertEqual(len(cached_files), 1)
        self.assertNotIn("theorem bad", cached_text)
        self.assertIn("theorem good", cached_text)


if __name__ == "__main__":
    unittest.main()
