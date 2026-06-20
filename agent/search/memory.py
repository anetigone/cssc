"""Self-managed compact memory for the minimal proof-refinement loop.

Phase 1 replaces the fixed stack of full attempt history in the prompt with a
small, durable :class:`ProofMemory` that the controller updates after every
check. The memory carries provenance (which attempts fed it) and never promotes
a model's claim into ``established_facts`` unless the checker actually accepted
a local conclusion.

The :class:`MemoryProcessor` is deterministic: it reads the latest attempt's
proposal, checker feedback and the previous memory, then returns a replacement.
A model-driven rewriter is deliberately out of scope for Phase 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProofTask,
)


logger = logging.getLogger(__name__)


_API_LESSON_CATEGORIES = frozenset(
    {
        DiagnosticCategory.UNKNOWN_IDENTIFIER,
        DiagnosticCategory.INVALID_REFERENCE,
        DiagnosticCategory.TYPE_MISMATCH,
    }
)

# Cap each list so the compact memory never grows unbounded across a long run.
_MAX_ESTABLISHED_FACTS = 12
_MAX_FAILED_APPROACHES = 12
_MAX_API_LESSONS = 8
_MAX_OPEN_GOALS = 6
_MAX_SOURCE_ATTEMPT_IDS = 24


@dataclass(frozen=True)
class ProofMemory:
    """Compact, provenance-carrying notes passed back into the proof prompt.

    Field shape follows the Phase 1 design note. ``established_facts`` only
    ever holds checker-verified conclusions; model claims live in the trace,
    not here.
    """

    established_facts: tuple[str, ...] = ()
    failed_approaches: tuple[str, ...] = ()
    lean_api_lessons: tuple[str, ...] = ()
    open_goals: tuple[str, ...] = ()
    next_strategy: str | None = None
    source_attempt_ids: tuple[int, ...] = ()


def empty_memory() -> ProofMemory:
    """Return the initial memory for a fresh controller run."""
    return ProofMemory()


def memory_to_prompt(memory: ProofMemory) -> str:
    """Render the memory as a terse block for the proof-generation prompt.

    Empty fields are omitted so the first iteration (empty memory) adds no
    noise to the prompt.
    """
    lines: list[str] = []
    if memory.established_facts:
        lines.append("Established facts (checker-verified):")
        lines.extend(f"- {fact}" for fact in memory.established_facts)
    if memory.failed_approaches:
        lines.append("Approaches that already failed:")
        lines.extend(f"- {approach}" for approach in memory.failed_approaches)
    if memory.lean_api_lessons:
        lines.append("Lean API lessons:")
        lines.extend(f"- {lesson}" for lesson in memory.lean_api_lessons)
    if memory.open_goals:
        lines.append("Open goals:")
        lines.extend(f"- {goal}" for goal in memory.open_goals)
    if memory.next_strategy:
        lines.append(f"Next strategy: {memory.next_strategy}")
    if not lines:
        return ""
    return "\n".join(lines)


def memory_to_dict(memory: ProofMemory) -> dict[str, Any]:
    """Trace-friendly snapshot of the memory fields."""
    return {
        "established_facts": list(memory.established_facts),
        "failed_approaches": list(memory.failed_approaches),
        "lean_api_lessons": list(memory.lean_api_lessons),
        "open_goals": list(memory.open_goals),
        "next_strategy": memory.next_strategy,
        "source_attempt_ids": list(memory.source_attempt_ids),
    }


@dataclass(frozen=True)
class MemoryUpdate:
    """Inputs for one deterministic memory update."""

    task: ProofTask
    attempt_index: int
    proof_text: str
    action: str
    check_result: CheckResult
    feedback: ParsedFeedback | None = None
    # Override for the outcome the memory should record. Defaults to the
    # checker's verdict; a caller that runs an additional safety review sets
    # this to ``False`` when a checker-accepted candidate is rejected, so the
    # memory never promotes a shortcut into ``established_facts``.
    effective_accepted: bool | None = None
    safety_reasons: tuple[str, ...] = ()


class MemoryProcessor:
    """Deterministically fold one attempt's outcome into the compact memory."""

    def update(self, memory: ProofMemory, update: MemoryUpdate) -> ProofMemory:
        established_facts = memory.established_facts
        failed_approaches = memory.failed_approaches
        lean_api_lessons = memory.lean_api_lessons
        open_goals: tuple[str, ...] = ()
        feedback = update.feedback or update.check_result.parsed_feedback

        accepted = (
            update.effective_accepted
            if update.effective_accepted is not None
            else update.check_result.accepted
        )
        if accepted:
            established_facts = self._record_established_fact(
                established_facts, update
            )
        else:
            failed_approaches = self._record_failed_approach(
                failed_approaches, update, feedback
            )
            lean_api_lessons = self._record_api_lesson(
                lean_api_lessons, feedback
            )

        open_goals = self._open_goals(feedback)

        source_attempt_ids = _append_unique(
            memory.source_attempt_ids, update.attempt_index
        )[-_MAX_SOURCE_ATTEMPT_IDS:]

        updated = ProofMemory(
            established_facts=established_facts[-_MAX_ESTABLISHED_FACTS:],
            failed_approaches=failed_approaches[-_MAX_FAILED_APPROACHES:],
            lean_api_lessons=lean_api_lessons[-_MAX_API_LESSONS:],
            open_goals=open_goals[-_MAX_OPEN_GOALS:],
            # Deterministic processor never infers a strategy; a model-driven
            # rewriter may set this in a later phase.
            next_strategy=None,
            source_attempt_ids=source_attempt_ids,
        )
        logger.debug(
            "Memory updated: attempt_index=%d accepted=%s "
            "established_facts=%d failed_approaches=%d open_goals=%d",
            update.attempt_index,
            accepted,
            len(updated.established_facts),
            len(updated.failed_approaches),
            len(updated.open_goals),
        )
        return updated

    def _record_established_fact(
        self,
        established_facts: tuple[str, ...],
        update: MemoryUpdate,
    ) -> tuple[str, ...]:
        # Minimal loop proves one obligation; record the task id as the proven
        # conclusion with its provenance attempt. This never invents a fact the
        # checker did not verify.
        fact = f"main goal proven (task={update.task.task_id}, attempt={update.attempt_index})"
        return _append_unique(established_facts, fact)

    def _record_failed_approach(
        self,
        failed_approaches: tuple[str, ...],
        update: MemoryUpdate,
        feedback: ParsedFeedback | None,
    ) -> tuple[str, ...]:
        if update.safety_reasons:
            note = "safety_rejected:" + ",".join(update.safety_reasons)
            return _append_unique(failed_approaches, note)
        category = (
            feedback.category.value
            if feedback is not None
            else update.check_result.category.value
        )
        goal_head = ""
        if feedback is not None and feedback.goal_state:
            goal_head = feedback.goal_state[0].goal_fingerprint
        note = (
            f"{update.action}:{category}"
            + (f":goal={goal_head}" if goal_head else "")
        )
        return _append_unique(failed_approaches, note)

    def _record_api_lesson(
        self,
        lean_api_lessons: tuple[str, ...],
        feedback: ParsedFeedback | None,
    ) -> tuple[str, ...]:
        if feedback is None or feedback.category not in _API_LESSON_CATEGORIES:
            return lean_api_lessons
        message = (feedback.message or "").strip()
        if not message:
            return lean_api_lessons
        lesson = f"{feedback.category.value}: {message}"
        return _append_unique(lean_api_lessons, lesson)

    def _open_goals(self, feedback: ParsedFeedback | None) -> tuple[str, ...]:
        if feedback is None:
            return ()
        goals = tuple(
            state.text.strip()
            for state in feedback.goal_state
            if state.text and state.text.strip()
        )
        # Prefer structured goal_state; fall back to legacy text for non-Lean
        # backends that only populate unsolved_goals.
        if not goals:
            goals = tuple(
                goal.strip()
                for goal in feedback.unsolved_goals
                if goal and goal.strip()
            )
        # Dedupe while preserving order.
        return tuple(dict.fromkeys(goals))


def _append_unique(items: tuple[Any, ...], item: Any) -> tuple[Any, ...]:
    if item in items:
        return items
    return (*items, item)
