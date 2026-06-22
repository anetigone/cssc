from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from agent.runtime.workspace import AttemptWorkspace
from agent.search.action import ActionCandidate, ActionGenerationRequest
from agent.search.budget import BudgetConfig
from agent.search.controller import ControllerConfig
from agent.search.execution import ExecutionMode
from agent.search.safety import SafetyVerdict
from agent.search.structured import StructuredController


def _task() -> ProofTask:
    return ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")


class StructuredFakeAdapter(ProofSystemAdapter):
    """Adapter whose verdict depends on the candidate's proof text.

    ``trivial`` → accepted; ``stuck`` → unsolved with a fixed goal fingerprint
    (so stall / repair-fork detection fires); otherwise plain unsolved.
    """

    def __init__(self) -> None:
        self.checked_files: list[Path] = []

    def render_candidate(self, task: ProofTask, edit: CandidateEdit) -> str:
        return task.source_template.replace(task.hole_marker, edit.text)

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        self.checked_files.append(candidate_file)
        source = candidate_file.read_text(encoding="utf-8")
        if "trivial" in source:
            return CheckResult(
                accepted=True,
                category=DiagnosticCategory.PROOF_ACCEPTED,
                raw_output="",
                candidate_file=candidate_file,
                parsed_feedback=ParsedFeedback(
                    category=DiagnosticCategory.PROOF_ACCEPTED, message="ok"
                ),
            )
        if "stuck" in source:
            goal = GoalState(text="unsolved", goal_fingerprint="fp-stuck")
            return CheckResult(
                accepted=False,
                category=DiagnosticCategory.UNSOLVED_GOALS,
                raw_output="unsolved",
                candidate_file=candidate_file,
                parsed_feedback=ParsedFeedback(
                    category=DiagnosticCategory.UNSOLVED_GOALS,
                    message="unsolved",
                    goal_state=(goal,),
                ),
            )
        return CheckResult(
            accepted=False,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="unsolved",
            candidate_file=candidate_file,
            parsed_feedback=ParsedFeedback(
                category=DiagnosticCategory.UNSOLVED_GOALS, message="unsolved"
            ),
        )

    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        return ParsedFeedback(category=DiagnosticCategory.UNKNOWN, raw_output=raw_output)

    def extract_progress(
        self, parent_state: Any, check_result: CheckResult
    ) -> ProgressSignal:
        return ProgressSignal(diagnostic_category=check_result.category)


class QueueGenerator:
    """Pop one batch of proof texts per ``generate`` call."""

    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)
        self.requests: list[ActionGenerationRequest] = []

    def generate(self, request: ActionGenerationRequest):
        self.requests.append(request)
        if not self.batches:
            return []
        return [
            ActionCandidate(proof_text=text, action="queued")
            for text in self.batches.pop(0)
        ]


class RejectProofTextReviewer:
    def __init__(self, rejected_text: str) -> None:
        self.rejected_text = rejected_text

    def accepts(self, task, candidate_source, check_result) -> SafetyVerdict:
        del task, check_result
        if self.rejected_text in candidate_source:
            return SafetyVerdict(False, ("test_shortcut",))
        return SafetyVerdict(True)


class StructuredControllerTests(unittest.TestCase):
    def _controller(
        self,
        tmp: str,
        generator: QueueGenerator,
        *,
        budget: BudgetConfig | None = None,
        safety_reviewer: Any = None,
        adapter: ProofSystemAdapter | None = None,
        max_candidates: int = 1,
        retriever: Any = None,
        context_summarizer: Any = None,
    ) -> StructuredController:
        return StructuredController(
            adapter=adapter or StructuredFakeAdapter(),
            action_generator=generator,
            workspace=AttemptWorkspace(tmp),
            budget_config=budget or BudgetConfig(max_checks=8, max_model_calls=8),
            config=ControllerConfig(
                execution_mode=ExecutionMode.STRUCTURED,
                max_candidates_per_model_call=max_candidates,
            ),
            safety_reviewer=safety_reviewer,
            retriever=retriever,
            context_summarizer=context_summarizer,
        )

    def test_accepted_path_serializes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp, QueueGenerator([["trivial"]])
            )
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(result.stop_reason, "accepted")
        self.assertEqual(result.metrics.execution_mode, ExecutionMode.STRUCTURED)
        self.assertIn("workspace", result.metadata)
        # The workspace snapshot records the accepted root branch.
        workspace = result.metadata["workspace"]
        self.assertTrue(any(b["status"] == "accepted" for b in workspace["branches"]))
        # Assembly consumed one extra check on top of the single attempt.
        self.assertEqual(result.metrics.budget_checks_used, 2)

    def test_budget_exhaustion_returns_unaccepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail"], ["fail"], ["fail"], ["fail"]]),
                budget=BudgetConfig(max_checks=2, max_model_calls=2),
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        self.assertTrue(result.stop_reason.startswith("budget"))
        self.assertEqual(len(result.attempts), 2)

    def test_repair_child_spawns_on_repeated_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                # Three identical stalled attempts: the second spawns a REPAIR
                # child (root-branch.r0), the third retires the parent to
                # DORMANT. The child has no generator batches left, so the run
                # ends with no_actions.
                QueueGenerator([["stuck"], ["stuck"], ["stuck"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        branch_ids = {b["branch_id"] for b in result.metadata["workspace"]["branches"]}
        self.assertIn("sample:root.r0", branch_ids)

    def test_safety_rejection_keeps_branch_active(self) -> None:
        reviewer = RejectProofTextReviewer("trivial")
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["trivial"], ["fail"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
                safety_reviewer=reviewer,
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        # The first attempt was checker-accepted but safety-rejected; that is
        # recorded as a safety rejection, not a successful assembly.
        self.assertTrue(result.metadata["safety_rejections"])

    def test_tool_unavailable_short_circuits(self) -> None:
        class ToolUnavailableAdapter(StructuredFakeAdapter):
            def check(self, candidate_file, budget_slice):
                return CheckResult(
                    accepted=False,
                    category=DiagnosticCategory.TOOL_UNAVAILABLE,
                    raw_output="no lean",
                    candidate_file=candidate_file,
                )

        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
                adapter=ToolUnavailableAdapter(),
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "tool_unavailable")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.metrics.budget_checks_used, 1)
        observations = result.metadata["workspace"]["branches"][0]["observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["category"], "tool_unavailable")

    def test_checks_all_candidates_and_preserves_or_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail", "trivial"]]),
                max_candidates=2,
                budget=BudgetConfig(max_checks=3, max_model_calls=1),
            )
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.attempts), 2)
        branches = result.metadata["workspace"]["branches"]
        self.assertEqual(len(branches), 2)
        self.assertTrue(any(branch["parent_branch_id"] for branch in branches))

    def test_final_assembly_inserts_proof_snippet_only_once(self) -> None:
        class StrictSourceAdapter(StructuredFakeAdapter):
            def check(self, candidate_file, budget_slice):
                source = candidate_file.read_text(encoding="utf-8")
                if source.count("theorem sample") != 1:
                    return CheckResult(
                        accepted=False,
                        category=DiagnosticCategory.ELABORATION_ERROR,
                        raw_output="nested theorem",
                        candidate_file=candidate_file,
                    )
                return super().check(candidate_file, budget_slice)

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                QueueGenerator([["trivial"]]),
                adapter=StrictSourceAdapter(),
            ).run(_task())

        self.assertTrue(result.accepted)

    def test_no_actions_blocks_only_selected_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, QueueGenerator([]))
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(
            result.metadata["workspace"]["branches"][0]["status"],
            "blocked",
        )

    def test_assembly_reserves_its_own_check_budget(self) -> None:
        # max_checks=2: one attempt + the assembly recheck. The run must still
        # reach assembly because has_complete_solution fires after attempt 1.
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["trivial"]]),
                budget=BudgetConfig(max_checks=2, max_model_calls=2),
            )
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(result.metrics.budget_checks_used, 2)

    def test_rejects_non_structured_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                StructuredController(
                    adapter=StructuredFakeAdapter(),
                    action_generator=QueueGenerator([]),
                    workspace=AttemptWorkspace(tmp),
                    config=ControllerConfig(execution_mode=ExecutionMode.MINIMAL),
                )


if __name__ == "__main__":
    unittest.main()
