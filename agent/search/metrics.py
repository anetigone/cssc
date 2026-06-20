"""Baseline measurement primitives for the proof-search loop.

Phase 0 of the redesign fixes the evaluation vocabulary so later phases can be
ablated against a stable baseline. Everything here is *observational*: it
summarizes what the existing linear loop already does without changing its
behavior. The two anchors are:

- a stable ``goal_fingerprint`` for a checker goal state, so repeated failures
  on the same goal can be detected and de-duplicated;
- a ``RunMetrics`` roll-up produced from a controller result, exposing the
  success/failure/stall semantics a single run is supposed to have.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
)


_WHITESPACE_RE = re.compile(r"\s+", re.MULTILINE)
_GOAL_SEPARATOR_RE = re.compile(r"\n\s*\n")


def normalize_goal_text(goal: str) -> str:
    """Collapse whitespace and trim a single goal block for stable hashing.

    Lean goal output carries line/column noise and incidental indentation that
    must not fragment the fingerprint of the same logical goal.
    """
    if not goal:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", goal.strip())
    return collapsed


def goal_fingerprint(goal: str) -> str:
    """Return a short stable identifier for a single goal block.

    Returns an empty string for an empty goal so callers can distinguish "no
    goal captured" from a real fingerprint.
    """
    normalized = normalize_goal_text(goal)
    if not normalized:
        return ""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return digest[:12]


def goal_fingerprints(goals: Sequence[str]) -> tuple[str, ...]:
    """Fingerprint each goal block, preserving order and multiplicity."""
    return tuple(goal_fingerprint(goal) for goal in goals if goal and goal.strip())


def feedback_goal_fingerprint(feedback: ParsedFeedback | None) -> str | None:
    """Fingerprint the first unsolved goal of a feedback, or None when absent.

    The first goal is the one the model actually needs to discharge, so it is
    the right signal for de-duplicating repeated stalls.
    """
    if feedback is None or not feedback.unsolved_goals:
        return None
    fingerprint = goal_fingerprint(feedback.unsolved_goals[0])
    return fingerprint or None


@dataclass(frozen=True)
class AttemptMetric:
    """Per-attempt observation used for baseline measurement and ablation.

    This is a compact, serializable projection of an ``AttemptRecord``. It keeps
    exactly the fields Phase 0 says the loop must record every round: the
    attempt index, the proposed proof, the checker category, the goal
    fingerprint, and whether the attempt made progress.
    """

    attempt_index: int
    action: str
    category: str
    accepted: bool
    goal_fingerprint: str | None
    error_message: str
    progressed: bool
    elapsed_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)


def attempt_metric(
    attempt_index: int,
    *,
    action: str,
    check_result: CheckResult,
    progressed: bool,
) -> AttemptMetric:
    """Project a checked candidate into a baseline measurement row."""
    feedback = check_result.parsed_feedback
    return AttemptMetric(
        attempt_index=attempt_index,
        action=action,
        category=check_result.category.value,
        accepted=check_result.accepted,
        goal_fingerprint=feedback_goal_fingerprint(feedback),
        error_message=feedback.message if feedback else "",
        progressed=progressed,
        elapsed_seconds=check_result.elapsed_seconds,
    )


@dataclass(frozen=True)
class RunMetrics:
    """Roll-up of one controller run against the Phase 0 evaluation contract.

    Captures the fixed success/failure/stall semantics:

    - ``accepted`` is the only success outcome;
    - ``stop_reason`` records *why* the loop ended, distinguishing a clean
      acceptance from budget exhaustion, generator exhaustion, or an unavailable
      checker;
    - ``pass_at_k`` separates a single iterative run (k=1) from independent
      repeated runs, so a "stuck but iterating" run is not silently scored as
      pass@k success;
    - ``distinct_goal_fingerprints`` and ``repeated_goal_stalls`` expose whether
      the loop is exploring new goals or grinding on the same one.
    """

    accepted: bool
    stop_reason: str
    pass_at_k: int
    attempts: tuple[AttemptMetric, ...]
    distinct_goal_fingerprints: int
    repeated_goal_stalls: int
    budget_checks_used: int
    budget_model_calls_used: int
    budget_exhausted_reason: str | None


def summarize_run(
    *,
    accepted: bool,
    stop_reason: str,
    attempts: Sequence[AttemptMetric],
    pass_at_k: int = 1,
    budget_checks_used: int,
    budget_model_calls_used: int,
    budget_exhausted_reason: str | None,
) -> RunMetrics:
    """Assemble a run roll-up from per-attempt metrics.

    ``pass_at_k`` defaults to ``1``: a single iterative controller run is one
    sample, not k independent attempts. Callers running the same task multiple
    times set it explicitly so the metric is never silently inflated.
    """
    fingerprints: list[str] = []
    stalls: dict[str, int] = {}
    repeated_stalls = 0
    for metric in attempts:
        if metric.accepted or metric.goal_fingerprint is None:
            continue
        fingerprints.append(metric.goal_fingerprint)
        seen = stalls.get(metric.goal_fingerprint, 0)
        stalls[metric.goal_fingerprint] = seen + 1
        if seen >= 1:
            repeated_stalls += 1
    return RunMetrics(
        accepted=accepted,
        stop_reason=stop_reason,
        pass_at_k=max(1, pass_at_k),
        attempts=tuple(attempts),
        distinct_goal_fingerprints=len(set(fingerprints)),
        repeated_goal_stalls=repeated_stalls,
        budget_checks_used=budget_checks_used,
        budget_model_calls_used=budget_model_calls_used,
        budget_exhausted_reason=budget_exhausted_reason,
    )


def run_metrics_payload(metrics: RunMetrics) -> dict[str, Any]:
    """Render a run roll-up as a trace-friendly JSON dictionary."""
    return {
        "accepted": metrics.accepted,
        "stop_reason": metrics.stop_reason,
        "pass_at_k": metrics.pass_at_k,
        "attempt_count": len(metrics.attempts),
        "distinct_goal_fingerprints": metrics.distinct_goal_fingerprints,
        "repeated_goal_stalls": metrics.repeated_goal_stalls,
        "budget_checks_used": metrics.budget_checks_used,
        "budget_model_calls_used": metrics.budget_model_calls_used,
        "budget_exhausted_reason": metrics.budget_exhausted_reason,
        "attempts": [_attempt_metric_payload(metric) for metric in metrics.attempts],
    }


def _attempt_metric_payload(metric: AttemptMetric) -> dict[str, Any]:
    return {
        "attempt_index": metric.attempt_index,
        "action": metric.action,
        "category": metric.category,
        "accepted": metric.accepted,
        "goal_fingerprint": metric.goal_fingerprint,
        "error_message": metric.error_message,
        "progressed": metric.progressed,
        "elapsed_seconds": metric.elapsed_seconds,
        "metadata": metric.metadata,
    }


_STALL_CATEGORIES = frozenset(
    {
        DiagnosticCategory.UNSOLVED_GOALS.value,
        DiagnosticCategory.TACTIC_FAILED.value,
        DiagnosticCategory.TYPE_MISMATCH.value,
    }
)


def is_stall_category(category: str) -> bool:
    """Whether a checker category represents a goal the loop stalled on.

    These are the categories that carry an unsolved-goal fingerprint; a parser
    error or a timeout is a different failure mode and should not be folded
    into goal-stall accounting.
    """
    return category in _STALL_CATEGORIES
