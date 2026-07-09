"""Frozen cost semantics for cross-mode observation (Phase 8.0).

A :class:`CostVector` is a small, frozen, serializable roll-up of the resources
one proof-search run (or, later, one branch/obligation) actually consumed. It is
deliberately a *projection* of existing observations --- :class:`RunMetrics`,
:class:`BudgetSnapshot` and :class:`AttemptMetric` --- never written back to
them, so nothing about checker / safety / frontier / reducer semantics changes.

Both ``minimal`` and ``structured`` share this module; it lives in the shared
``agent/search`` layer and imports nothing from the ``structured`` subpackage.
Token accounting reuses the existing convention: ``input_tokens`` is the prompt
total, ``output_tokens`` is the visible completion (hidden provider reasoning
stays out, recorded only under ``metadata["model_usage"]``). ``elapsed_ms`` is
the run-level wall-clock from :class:`BudgetManager`, not the sum of per-check
times.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .budget import BudgetSnapshot
from .metrics import AttemptMetric, RunMetrics


@dataclass(frozen=True)
class CostVector:
    """Comparable resource counters for one run, branch, or obligation."""

    model_calls: int = 0
    checks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0


def zero_cost() -> CostVector:
    """Return the additive identity cost vector."""
    return CostVector()


def add_cost(left: CostVector, right: CostVector) -> CostVector:
    """Add two cost vectors field by field, returning a new frozen vector."""
    return CostVector(
        model_calls=left.model_calls + right.model_calls,
        checks=left.checks + right.checks,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        elapsed_ms=left.elapsed_ms + right.elapsed_ms,
    )


def to_dict(cost: CostVector) -> dict[str, Any]:
    """Render a cost vector as a trace-friendly JSON dictionary."""
    return {
        "model_calls": cost.model_calls,
        "checks": cost.checks,
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
        "elapsed_ms": cost.elapsed_ms,
    }


def cost_vector_from_dict(data: dict[str, Any]) -> CostVector:
    """Reconstruct a cost vector from :func:`to_dict` output."""
    return CostVector(
        model_calls=int(data.get("model_calls", 0)),
        checks=int(data.get("checks", 0)),
        input_tokens=int(data.get("input_tokens", 0)),
        output_tokens=int(data.get("output_tokens", 0)),
        elapsed_ms=int(data.get("elapsed_ms", 0)),
    )


def cost_vector_from_metrics_and_budget(
    metrics: RunMetrics | None,
    snapshot: BudgetSnapshot,
) -> CostVector:
    """Derive the run-level cost vector from a metrics roll-up and budget snapshot.

    ``elapsed_ms`` comes from the budget's run wall-clock (covering model waits,
    safety review and final assembly), not from per-check time. ``metrics`` is
    optional only to stay robust where a result is built without one.
    """
    if metrics is None:
        model_calls = 0
        checks = 0
        input_tokens = 0
        output_tokens = 0
    else:
        model_calls = metrics.budget_model_calls_used
        checks = metrics.budget_checks_used
        input_tokens = metrics.model_input_tokens
        output_tokens = metrics.model_output_tokens
    return CostVector(
        model_calls=model_calls,
        checks=checks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        elapsed_ms=round(snapshot.elapsed_seconds * 1000),
    )


def cost_vector_from_attempt(attempt: AttemptMetric) -> CostVector:
    """Derive the per-attempt cost vector.

    Attempts carry no token or call counters (those are run-level), so only the
    check wall-clock is populated. Kept here for Phase 8.1 branch attribution.
    """
    return CostVector(elapsed_ms=round(attempt.elapsed_seconds * 1000))
