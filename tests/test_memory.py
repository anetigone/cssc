from __future__ import annotations

import unittest

from agent.proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
)
from agent.search.memory import (
    MemoryProcessor,
    MemoryUpdate,
    ProofMemory,
    empty_memory,
    memory_to_prompt,
)
from agent.tasks.types import ProofTask


def _task() -> ProofTask:
    return ProofTask("demo", "theorem demo : True := by\n  {{proof}}\n")


def _update(
    task: ProofTask,
    *,
    attempt_index: int,
    accepted: bool,
    feedback: ParsedFeedback | None,
    action: str = "model_complete",
) -> MemoryUpdate:
    category = (
        feedback.category
        if feedback is not None
        else (DiagnosticCategory.PROOF_ACCEPTED if accepted else DiagnosticCategory.UNKNOWN)
    )
    result = CheckResult(
        accepted=accepted,
        category=category,
        raw_output="raw",
        parsed_feedback=feedback,
    )
    return MemoryUpdate(
        task=task,
        attempt_index=attempt_index,
        proof_text="some proof",
        action=action,
        check_result=result,
        feedback=feedback,
    )


class MemoryProcessorTests(unittest.TestCase):
    def test_safety_rejection_overrides_checker_acceptance(self) -> None:
        task = _task()
        update = _update(task, attempt_index=0, accepted=True, feedback=None)
        update = MemoryUpdate(
            **{
                **update.__dict__,
                "effective_accepted": False,
                "safety_reasons": ("residual_shortcut:sorry",),
            }
        )

        memory = MemoryProcessor().update(empty_memory(), update)

        self.assertEqual(memory.established_facts, ())
        self.assertIn(
            "safety_rejected:residual_shortcut:sorry",
            memory.failed_approaches,
        )

    def test_records_established_fact_only_when_checker_accepts(self) -> None:
        task = _task()
        processor = MemoryProcessor()
        memory = empty_memory()

        # A failed attempt must not promote anything into established_facts.
        failed_feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            goal_state=(GoalState(text="⊢ True", goal_fingerprint="abc"),),
        )
        memory = processor.update(
            memory, _update(task, attempt_index=0, accepted=False, feedback=failed_feedback)
        )
        self.assertEqual(memory.established_facts, ())
        self.assertTrue(memory.failed_approaches)
        self.assertEqual(memory.open_goals, ("⊢ True",))

        # The checker-accepted attempt records the proven conclusion.
        accepted_feedback = ParsedFeedback(
            category=DiagnosticCategory.PROOF_ACCEPTED,
            message="accepted",
        )
        memory = processor.update(
            memory, _update(task, attempt_index=1, accepted=True, feedback=accepted_feedback)
        )
        self.assertTrue(memory.established_facts)
        self.assertIn("task=demo", memory.established_facts[-1])
        self.assertEqual(memory.source_attempt_ids, (0, 1))

    def test_captures_lean_api_lessons_for_identifier_errors(self) -> None:
        task = _task()
        processor = MemoryProcessor()
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
            message="unknown identifier 'fooBar'",
        )
        memory = processor.update(
            empty_memory(), _update(task, attempt_index=0, accepted=False, feedback=feedback)
        )
        self.assertTrue(memory.lean_api_lessons)
        self.assertIn("fooBar", memory.lean_api_lessons[0])

    def test_does_not_duplicate_failed_approaches_or_attempt_ids(self) -> None:
        task = _task()
        processor = MemoryProcessor()
        feedback = ParsedFeedback(
            category=DiagnosticCategory.UNSOLVED_GOALS,
            message="unsolved goals",
            goal_state=(GoalState(text="⊢ True", goal_fingerprint="abc"),),
        )
        update = _update(task, attempt_index=0, accepted=False, feedback=feedback)
        memory = processor.update(processor.update(empty_memory(), update), update)
        self.assertEqual(len(memory.failed_approaches), 1)
        self.assertEqual(memory.source_attempt_ids, (0,))

    def test_prompt_omits_empty_memory_and_lists_nonempty_fields(self) -> None:
        self.assertEqual(memory_to_prompt(empty_memory()), "")

        populated = ProofMemory(
            established_facts=("main goal proven",),
            open_goals=("⊢ True",),
        )
        rendered = memory_to_prompt(populated)
        self.assertIn("Established facts", rendered)
        self.assertIn("Open goals", rendered)
        self.assertNotIn("Lean API lessons", rendered)


if __name__ == "__main__":
    unittest.main()
