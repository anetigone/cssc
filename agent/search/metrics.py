"""Raw baseline observations for the existing linear proof loop.

Phase 0 deliberately records facts without inferring progress, stalls, parent
relationships, or pass@k. Those semantics belong to later evaluation and
search-policy layers, where they can be defined against real traces.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..proof_system.base import CheckResult


_WHITESPACE_RE = re.compile(r"\s+", re.MULTILINE)


def normalize_goal_text(goal: str) -> str:
    """Collapse incidental whitespace in one captured Lean goal."""
    if not goal:
        return ""
    return _WHITESPACE_RE.sub(" ", goal.strip())


def goal_fingerprint(goal: str) -> str:
    """Return a stable short identifier for one non-empty goal."""
    normalized = normalize_goal_text(goal)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def goal_fingerprints(goals: Sequence[str]) -> tuple[str, ...]:
    """Fingerprint all captured goals, preserving order and multiplicity."""
    return tuple(goal_fingerprint(goal) for goal in goals if goal and goal.strip())


def new_sample_id() -> str:
    """Generate a unique identifier for one controller run."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class AttemptMetric:
    """Raw observation of one checked candidate."""

    attempt_index: int
    action: str
    category: str
    accepted: bool
    goal_fingerprints: tuple[str, ...]
    error_message: str
    elapsed_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)


def attempt_metric(
    attempt_index: int,
    *,
    action: str,
    check_result: CheckResult,
) -> AttemptMetric:
    """Project a checker result without interpreting relationships to other attempts."""
    feedback = check_result.parsed_feedback
    goals = feedback.unsolved_goals if feedback is not None else ()
    return AttemptMetric(
        attempt_index=attempt_index,
        action=action,
        category=check_result.category.value,
        accepted=check_result.accepted,
        goal_fingerprints=goal_fingerprints(goals),
        error_message=feedback.message if feedback else "",
        elapsed_seconds=check_result.elapsed_seconds,
    )


@dataclass(frozen=True)
class RunMetrics:
    """Raw roll-up for one independent controller run."""

    sample_id: str
    task_id: str
    accepted: bool
    stop_reason: str
    attempts: tuple[AttemptMetric, ...]
    budget_checks_used: int
    budget_model_calls_used: int
    budget_exhausted_reason: str | None


def summarize_run(
    *,
    sample_id: str,
    task_id: str,
    accepted: bool,
    stop_reason: str,
    attempts: Sequence[AttemptMetric],
    budget_checks_used: int,
    budget_model_calls_used: int,
    budget_exhausted_reason: str | None,
) -> RunMetrics:
    """Assemble one run's raw observations."""
    return RunMetrics(
        sample_id=sample_id,
        task_id=task_id,
        accepted=accepted,
        stop_reason=stop_reason,
        attempts=tuple(attempts),
        budget_checks_used=budget_checks_used,
        budget_model_calls_used=budget_model_calls_used,
        budget_exhausted_reason=budget_exhausted_reason,
    )


def run_metrics_payload(metrics: RunMetrics) -> dict[str, Any]:
    """Render a run roll-up as a trace-friendly JSON dictionary."""
    return {
        "sample_id": metrics.sample_id,
        "task_id": metrics.task_id,
        "accepted": metrics.accepted,
        "stop_reason": metrics.stop_reason,
        "attempt_count": len(metrics.attempts),
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
        "goal_fingerprints": list(metric.goal_fingerprints),
        "error_message": metric.error_message,
        "elapsed_seconds": metric.elapsed_seconds,
        "metadata": metric.metadata,
    }
