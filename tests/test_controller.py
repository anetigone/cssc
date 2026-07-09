from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.search.action import (
    ActionCandidate,
    ActionGenerationError,
    ActionGenerationRequest,
)
from agent.search.budget import BudgetConfig
from agent.search.controller import ControllerConfig, ProofController
from agent.search.safety import SafetyVerdict
from agent.retrieval import RetrievalResult
from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from agent.runtime.workspace import AttemptWorkspace
from agent.runtime.workspace import EphemeralCheckWorkspace


class FakeAdapter(ProofSystemAdapter):
    def __init__(self) -> None:
        self.checked_files: list[Path] = []

    def render_candidate(self, task: ProofTask, candidate_edit: CandidateEdit) -> str:
        return task.source_template.replace(task.hole_marker, candidate_edit.text)

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        self.checked_files.append(candidate_file)
        source = candidate_file.read_text(encoding="utf-8")
        if "trivial" in source:
            feedback = ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED,
                message="accepted",
            )
            return CheckResult(
                accepted=True,
                category=DiagnosticCategory.PROOF_ACCEPTED,
                raw_output="",
                candidate_file=candidate_file,
                parsed_feedback=feedback,
            )
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            raw_output="unsolved goals",
        )
        return CheckResult(
            accepted=False,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="unsolved goals",
            candidate_file=candidate_file,
            parsed_feedback=feedback,
        )

    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        return ParsedFeedback(category=DiagnosticCategory.UNKNOWN, raw_output=raw_output)

    def extract_progress(
        self,
        parent_state: Any,
        check_result: CheckResult,
    ) -> ProgressSignal:
        return ProgressSignal(diagnostic_category=check_result.category)


class QueueGenerator:
    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = batches
        self.requests: list[ActionGenerationRequest] = []

    def generate(self, request: ActionGenerationRequest) -> list[ActionCandidate]:
        self.requests.append(request)
        if not self.batches:
            return []
        return [ActionCandidate(proof_text=text, action="queued") for text in self.batches.pop(0)]


class FakeRetriever:
    def __init__(self) -> None:
        self.requests: list[tuple[ProofTask | None, ParsedFeedback | None, int]] = []

    def retrieve(
        self,
        query: str | None = None,
        *,
        task: ProofTask | None = None,
        feedback: ParsedFeedback | None = None,
        top_k: int = 5,
    ) -> tuple[RetrievalResult, ...]:
        self.requests.append((task, feedback, top_k))
        return (
            RetrievalResult(
                name="true_intro",
                source_path="Demo.lean",
                start_line=1,
                snippet="theorem true_intro : True := by\n  trivial",
                score=0.7,
            ),
        )


class FakeSummarizer:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def summarize(self, request: Any) -> Any:
        self.requests.append(request)
        from agent.agents.context import SummarizationResult

        return SummarizationResult(
            concise_error="summarized: unsolved goals",
            strategy_hint="try trivial",
            was_summarized=True,
        )


class RejectProofTextReviewer:
    def __init__(self, rejected_text: str) -> None:
        self.rejected_text = rejected_text
        self.calls: list[str] = []

    def accepts(self, task, candidate_source, check_result) -> SafetyVerdict:
        del task, check_result
        self.calls.append(candidate_source)
        if self.rejected_text in candidate_source:
            return SafetyVerdict(False, ("test_shortcut",))
        return SafetyVerdict(True)


class ProofControllerTests(unittest.TestCase):
    def test_retries_when_safety_rejects_checker_accepted_candidate(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["trivial -- unsafe"], ["trivial"]])
        reviewer = RejectProofTextReviewer("unsafe")
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                safety_reviewer=reviewer,
                budget_config=BudgetConfig(max_checks=3, max_model_calls=3),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(
            result.metadata["safety_rejections"][0]["reasons"],
            ("test_shortcut",),
        )
        retry_memory = generator.requests[1].metadata["proof_memory"]
        self.assertEqual(retry_memory.established_facts, ())
        self.assertIn("safety_rejected:test_shortcut", retry_memory.failed_approaches)

    def test_does_not_call_safety_reviewer_for_checker_failure(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad"]])
        reviewer = RejectProofTextReviewer("bad")
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                safety_reviewer=reviewer,
                budget_config=BudgetConfig(max_checks=1, max_model_calls=1),
            )

            result = controller.run(task)

        self.assertFalse(result.accepted)
        self.assertEqual(reviewer.calls, [])

    def test_runs_until_candidate_is_accepted(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["exact False.elim"], ["trivial"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=4, max_model_calls=4),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        self.assertEqual(result.stop_reason, "accepted")
        self.assertIsNotNone(result.accepted_attempt)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.budget.checks_used, 2)
        # Every model call counts against the budget: one proposal + one retry.
        self.assertEqual(result.budget.model_calls_used, 2)
        self.assertEqual(len(generator.requests[1].previous_feedback), 1)
        previous_attempt = generator.requests[1].metadata["previous_attempt"]
        self.assertEqual(previous_attempt["proof_text"], "exact False.elim")
        self.assertEqual(previous_attempt["raw_output"], "unsolved goals")
        self.assertEqual(generator.requests[0].metadata["proof_phase"], "propose")
        self.assertEqual(generator.requests[1].metadata["proof_phase"], "retry")
        self.assertEqual(result.attempts[1].edit.metadata["proof_phase"], "retry")

    def test_retries_with_feedback_until_budget_exhausted(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad1"], ["bad2"], ["bad3"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=4, max_model_calls=2),
            )

            result = controller.run(task)

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "budget:model_calls")
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.budget.model_calls_used, 2)
        # Each retry carries the previous failure's feedback.
        self.assertEqual(
            [request.metadata["proof_phase"] for request in generator.requests],
            ["propose", "retry"],
        )

    def test_retrieves_context_for_model_request(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["trivial"]])
        retriever = FakeRetriever()
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                retriever=retriever,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=2, max_model_calls=2),
                config=ControllerConfig(retrieve_before_first_model_call=True),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        self.assertEqual(len(retriever.requests), 1)
        self.assertEqual(generator.requests[0].metadata["retrieved_results"][0].name, "true_intro")
        self.assertEqual(result.attempts[0].edit.metadata["retrieved_results"][0]["name"], "true_intro")

    def test_stops_when_check_budget_is_exhausted(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad"], ["still_bad"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=1, max_model_calls=3),
            )

            result = controller.run(task)

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "budget:checks")
        self.assertEqual(len(result.attempts), 1)

    def test_stops_when_generator_returns_no_actions(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([[]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=2, max_model_calls=2),
                config=ControllerConfig(max_candidates_per_model_call=2),
            )

            result = controller.run(task)

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(result.budget.model_calls_used, 1)
        self.assertEqual(result.budget.checks_used, 0)

    def test_generation_failure_is_not_reported_as_no_actions(self) -> None:
        class TruncatedGenerator:
            def generate(self, request):
                raise ActionGenerationError(
                    "model_output_truncated",
                    "reasoning consumed the response budget",
                    metadata={
                        "token_usage": {
                            "input_tokens": 10,
                            "output_tokens": 0,
                            "reasoning_tokens": 20,
                        }
                    },
                )

        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=TruncatedGenerator(),
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=2, max_model_calls=2),
            )
            result = controller.run(task)

        self.assertEqual(
            result.stop_reason, "generation:model_output_truncated"
        )
        self.assertEqual(result.metrics.model_input_tokens, 10)
        self.assertEqual(result.metrics.model_output_tokens, 0)
        self.assertEqual(
            result.metadata["generation_failures"][0]["reason"],
            "model_output_truncated",
        )

    def test_caps_feedback_history(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad1"], ["bad2"], ["bad3"], ["bad4"], ["bad5"], ["trivial"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=10, max_model_calls=10),
                config=ControllerConfig(max_feedback_history=3),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        self.assertLessEqual(len(generator.requests[-1].previous_feedback), 3)

    def test_records_archive_file_while_checking_project_local_copy(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["trivial"]])
        adapter = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "agent-runs"
            checker = root / "lean-project" / ".checks"
            controller = ProofController(
                adapter=adapter,
                action_generator=generator,
                workspace=AttemptWorkspace(archive),
                check_workspace=EphemeralCheckWorkspace(checker),
                budget_config=BudgetConfig(max_checks=2, max_model_calls=2),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        self.assertEqual(len(adapter.checked_files), 1)
        self.assertTrue(adapter.checked_files[0].is_relative_to(checker.resolve()))
        self.assertTrue(result.attempts[0].candidate_file.is_relative_to(archive.resolve()))
        self.assertEqual(result.attempts[0].check_result.candidate_file, result.attempts[0].candidate_file)
        self.assertFalse(adapter.checked_files[0].exists())
        self.assertFalse(adapter.checked_files[0].parent.exists())

    def test_calls_context_summarizer_on_retry(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad"], ["trivial"]])
        summarizer = FakeSummarizer()
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                context_summarizer=summarizer,
                budget_config=BudgetConfig(max_checks=3, max_model_calls=3),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        self.assertEqual(len(summarizer.requests), 1)
        self.assertEqual(summarizer.requests[0].attempt_index, 1)
        self.assertIn(
            "summarized: unsolved goals",
            generator.requests[1].metadata["summarized_context"].concise_error,
        )

    def test_carries_self_managed_proof_memory_across_retries(self) -> None:
        from agent.search.memory import ProofMemory

        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad"], ["trivial"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=3, max_model_calls=3),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        # First iteration ships an empty memory; the retry carries the memory
        # folded from the failed attempt.
        first_memory = generator.requests[0].metadata["proof_memory"]
        self.assertIsInstance(first_memory, ProofMemory)
        self.assertFalse(first_memory.failed_approaches)
        retry_memory = generator.requests[1].metadata["proof_memory"]
        self.assertIsInstance(retry_memory, ProofMemory)
        self.assertTrue(retry_memory.failed_approaches)
        self.assertIn(0, retry_memory.source_attempt_ids)

    def test_records_baseline_metrics_for_run(self) -> None:
        task = ProofTask("true", "theorem sample : True := by\n  {{proof}}\n")
        generator = QueueGenerator([["bad"], ["still_bad"], ["trivial"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = ProofController(
                adapter=FakeAdapter(),
                action_generator=generator,
                workspace=AttemptWorkspace(tmp),
                budget_config=BudgetConfig(max_checks=5, max_model_calls=5),
            )

            result = controller.run(task)

        self.assertTrue(result.accepted)
        metrics = result.metrics
        self.assertIsNotNone(metrics)
        self.assertTrue(metrics.accepted)
        self.assertEqual(metrics.stop_reason, "accepted")
        # Every controller run gets a unique trace id.
        self.assertTrue(metrics.sample_id)
        self.assertEqual(metrics.task_id, task.task_id)
        # Two failed attempts plus the accepted one.
        self.assertEqual(len(metrics.attempts), 3)
        self.assertFalse(metrics.attempts[0].accepted)
        self.assertTrue(metrics.attempts[-1].accepted)


if __name__ == "__main__":
    unittest.main()
