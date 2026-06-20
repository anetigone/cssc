"""Baseline measurement primitives for the proof-search loop.

Phase 0 of the redesign fixes the evaluation vocabulary so later phases can be
ablated against a stable baseline. Everything here is *observational*: it
summarizes what the existing linear loop already does without changing its
behavior. The anchors are:

- a stable ``goal_fingerprint`` for a single checker goal state, plus a
  ``GoalSetSnapshot`` that records the *full* ordered goal set per attempt;
- an ``AttemptMetric`` per-round projection comparing each attempt's goal set
  against the previous attempt's, so progress is derived from goal-set deltas
  rather than from error categories (which would mislabel repeated failures as
  progress);
- a ``RunMetrics`` roll-up produced from a controller result, exposing the
  success/failure/stall semantics a single run is supposed to have;
- an ``EvaluationAggregator`` that turns *k* independent runs of one task into
  pass@k. pass@k is a property of a *sample*, never of a single run, so a run
  records only ``accepted`` and its own ``sample_id``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
)


_WHITESPACE_RE = re.compile(r"\s+", re.MULTILINE)


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
    """Fingerprint each goal block, preserving order and dropping empties."""
    return tuple(goal_fingerprint(goal) for goal in goals if goal and goal.strip())


def new_sample_id() -> str:
    """Generate a unique identifier for one independent run.

    Used as ``RunMetrics.sample_id`` so an evaluation harness can group the k
    samples that feed a pass@k computation, even when their traces collide on
    task/attempt-count/stop-reason.
    """
    return uuid.uuid4().hex


@dataclass(frozen=True)
class GoalSetSnapshot:
    """Fingerprinted view of every unsolved goal produced by one attempt.

    Capturing the *whole* ordered goal set — not just the first goal — is what
    lets a multi-obligation task show that goal B repeated across attempts while
    goal A was discharged.
    """

    fingerprints: tuple[str, ...] = ()
    fingerprint_set: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_goals(cls, goals: Sequence[str]) -> GoalSetSnapshot:
        fingerprints = goal_fingerprints(goals)
        return cls(fingerprints=fingerprints, fingerprint_set=frozenset(fingerprints))

    @classmethod
    def from_feedback(cls, feedback: ParsedFeedback | None) -> GoalSetSnapshot:
        if feedback is None:
            return cls()
        return cls.from_goals(feedback.unsolved_goals)


@dataclass(frozen=True)
class GoalSetDelta:
    """Set-level comparison of an attempt's goals against the previous one.

    A negative ``goal_count_delta`` is real forward progress (fewer open goals);
    ``solved`` goals that do not reappear are also progress. An attempt that
    re-introduces a goal the parent had already closed is *not* progress even
    if the goal count dropped. ``retained`` goals that persist across attempts
    are the stall signal.
    """

    solved: frozenset[str]
    retained: frozenset[str]
    introduced: frozenset[str]
    goal_count_delta: int

    @property
    def made_progress(self) -> bool:
        # Discharging a goal without reintroducing it is forward motion.
        return bool(self.solved) and not self.introduced


def compare_goal_sets(
    parent: GoalSetSnapshot | None,
    child: GoalSetSnapshot,
) -> GoalSetDelta:
    """Compute the goal-set delta from a parent attempt to its child.

    ``parent`` is ``None`` for the first attempt, where everything in the child
    counts as introduced and nothing is solved.
    """
    if parent is None:
        return GoalSetDelta(
            solved=frozenset(),
            retained=frozenset(),
            introduced=child.fingerprint_set,
            goal_count_delta=len(child.fingerprints),
        )
    parent_set = parent.fingerprint_set
    child_set = child.fingerprint_set
    solved = parent_set - child_set
    retained = parent_set & child_set
    introduced = child_set - parent_set
    return GoalSetDelta(
        solved=solved,
        retained=retained,
        introduced=introduced,
        goal_count_delta=len(child.fingerprints) - len(parent.fingerprints),
    )


@dataclass(frozen=True)
class AttemptMetric:
    """Per-attempt observation used for baseline measurement and ablation.

    ``progressed`` is derived from the ``goal_delta`` (a goal-set comparison
    against the previous attempt), never from the error category, so repeated
    failures on the same goal are correctly recorded as no-progress stalls.
    """

    attempt_index: int
    action: str
    category: str
    accepted: bool
    goal_snapshot: GoalSetSnapshot
    goal_delta: GoalSetDelta
    progressed: bool
    error_message: str
    elapsed_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)


def attempt_metric(
    attempt_index: int,
    *,
    action: str,
    check_result: CheckResult,
    parent_snapshot: GoalSetSnapshot | None,
) -> AttemptMetric:
    """Project a checked candidate into a baseline measurement row.

    ``parent_snapshot`` is the previous attempt's goal set (or ``None`` for the
    first attempt). Progress is computed from the goal-set delta, so it does
    not depend on the checker category.
    """
    feedback = check_result.parsed_feedback
    snapshot = GoalSetSnapshot.from_feedback(feedback)
    delta = compare_goal_sets(parent_snapshot, snapshot)
    accepted = check_result.accepted
    progressed = accepted or delta.made_progress
    return AttemptMetric(
        attempt_index=attempt_index,
        action=action,
        category=check_result.category.value,
        accepted=accepted,
        goal_snapshot=snapshot,
        goal_delta=delta,
        progressed=progressed,
        error_message=feedback.message if feedback else "",
        elapsed_seconds=check_result.elapsed_seconds,
    )


@dataclass(frozen=True)
class RunMetrics:
    """Roll-up of one controller run against the Phase 0 evaluation contract.

    A single run records its own ``sample_id`` and ``accepted`` only. pass@k is
    *not* a single-run property: it is computed by ``EvaluationAggregator`` from
    k independent samples of the same task. Recording pass@k on a run would let
    one iterative run masquerade as k independent attempts.

    Stall accounting uses the full goal set: ``repeated_goal_stalls`` counts an
    attempt when it retains a goal its parent already had, and ``progressed``
    requires an actual goal-set improvement.
    """

    sample_id: str
    task_id: str
    accepted: bool
    stop_reason: str
    attempts: tuple[AttemptMetric, ...]
    distinct_goal_fingerprints: int
    repeated_goal_stalls: int
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
    """Assemble a run roll-up from per-attempt metrics.

    ``sample_id`` must be unique per independent run; generate it with
    ``new_sample_id()`` at run start. The same task run k times yields k
    samples with distinct sample ids for the aggregator.
    """
    fingerprints: list[str] = []
    repeated_stalls = 0
    for metric in attempts:
        if metric.accepted:
            continue
        # A retained goal (present in this attempt and the previous one) that
        # was not discharged is a stall on that goal.
        if metric.goal_delta.retained:
            repeated_stalls += 1
        fingerprints.extend(metric.goal_snapshot.fingerprints)
    return RunMetrics(
        sample_id=sample_id,
        task_id=task_id,
        accepted=accepted,
        stop_reason=stop_reason,
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
        "sample_id": metrics.sample_id,
        "task_id": metrics.task_id,
        "accepted": metrics.accepted,
        "stop_reason": metrics.stop_reason,
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
        "goal_fingerprints": list(metric.goal_snapshot.fingerprints),
        "solved_goals": sorted(metric.goal_delta.solved),
        "retained_goals": sorted(metric.goal_delta.retained),
        "introduced_goals": sorted(metric.goal_delta.introduced),
        "goal_count_delta": metric.goal_delta.goal_count_delta,
        "progressed": metric.progressed,
        "error_message": metric.error_message,
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
    """Whether a checker category is one that carries an unsolved-goal set.

    These are the categories whose parsed feedback exposes goal blocks; a
    parser error or a timeout is a different failure mode and is not folded
    into goal-stall accounting. Note this only classifies the category for
    reporting — it is NOT used to decide progress, which is goal-set based.
    """
    return category in _STALL_CATEGORIES


@dataclass(frozen=True)
class PassAtKResult:
    """Aggregated outcome of k independent samples of one task."""

    task_id: str
    k: int
    successes: int
    pass_at_k: float
    sample_ids: tuple[str, ...]


class EvaluationAggregator:
    """Turn k independent run metrics of one task into a pass@k estimate.

    pass@k is defined over *independent* samples: run the task k times, count
    how many succeeded, and pass@k is 1 if any succeeded (the unbiased
    estimator is more involved, but the binary "at least one" reading is what
    ablation comparisons need). This aggregator never looks inside a single
    run for k — each ``RunMetrics`` is exactly one sample.
    """

    def pass_at_k(self, samples: Iterable[RunMetrics]) -> PassAtKResult | None:
        samples = tuple(samples)
        if not samples:
            return None
        task_id = samples[0].task_id
        mismatched = {s.task_id for s in samples if s.task_id != task_id}
        if mismatched:
            raise ValueError(
                f"EvaluationAggregator received mixed tasks: {sorted(mismatched | {task_id})}"
            )
        successes = sum(1 for sample in samples if sample.accepted)
        k = len(samples)
        return PassAtKResult(
            task_id=task_id,
            k=k,
            successes=successes,
            pass_at_k=1.0 if successes > 0 else 0.0,
            sample_ids=tuple(sample.sample_id for sample in samples),
        )
