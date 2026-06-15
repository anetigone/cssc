from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.action import ActionCandidate, ActionGenerationRequest
from agent.budget import BudgetConfig
from agent.controller import ControllerConfig, ProofController
from agent.proof_system_adapter import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from agent.workspace import AttemptWorkspace


class FakeAdapter(ProofSystemAdapter):
    def render_candidate(self, task: ProofTask, candidate_edit: CandidateEdit) -> str:
        return task.source_template.replace(task.hole_marker, candidate_edit.text)

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
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


class ProofControllerTests(unittest.TestCase):
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
        self.assertEqual(result.budget.model_calls_used, 2)
        self.assertEqual(len(generator.requests[1].previous_feedback), 1)

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


if __name__ == "__main__":
    unittest.main()
