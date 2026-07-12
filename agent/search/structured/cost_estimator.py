"""Frozen-history action cost estimation.

This module consumes only explicitly completed action samples.  It neither
reads a live controller nor mutates a ledger, so a benchmark can freeze one
history snapshot before replaying any policy arm.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from statistics import median
from typing import Iterable, Mapping

from agent.proof_system.workspace import SearchActionKind
from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    MeasurementStatus,
)

from .action_frontier import CostEstimate, Estimate, static_execution_cost


_DIMENSIONS = (
    "model_requests", "input_tokens", "output_tokens", "billed_tokens",
    "checks", "checker_wall_ms", "checker_cpu_ms", "api_cost_usd",
)


@dataclass(frozen=True)
class CostBucket:
    """All context available at selection time for a history lookup."""

    model: str | None
    model_tier: str | None
    action_kind: SearchActionKind
    imports_profile: str
    goal_size_bucket: str
    obligation_size_bucket: str
    repair_state: str
    stalled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model, "model_tier": self.model_tier,
            "action_kind": self.action_kind.value, "imports_profile": self.imports_profile,
            "goal_size_bucket": self.goal_size_bucket,
            "obligation_size_bucket": self.obligation_size_bucket,
            "repair_state": self.repair_state, "stalled": self.stalled,
        }


def cost_bucket_from_dict(data: Mapping[str, object]) -> CostBucket:
    return CostBucket(
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        model_tier=data.get("model_tier") if isinstance(data.get("model_tier"), str) else None,
        action_kind=SearchActionKind(str(data["action_kind"])),
        imports_profile=str(data["imports_profile"]),
        goal_size_bucket=str(data["goal_size_bucket"]),
        obligation_size_bucket=str(data["obligation_size_bucket"]),
        repair_state=str(data["repair_state"]), stalled=bool(data["stalled"]),
    )


@dataclass(frozen=True)
class CompletedActionCost:
    """One terminal action observation eligible for historical estimation."""

    action_id: str
    bucket: CostBucket
    actual: CostEstimate
    completed_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.action_id or not self.completed_event_ids:
            raise ValueError("completed action costs need an id and completed ledger evidence")

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id, "bucket": self.bucket.to_dict(),
            "actual": self.actual.to_dict(),
            "completed_event_ids": list(self.completed_event_ids),
        }


@dataclass(frozen=True)
class CostHistorySnapshot:
    """Immutable, versioned historical observations used by one estimator."""

    snapshot_id: str
    samples: tuple[CompletedActionCost, ...]
    ledger_event_ids: tuple[str, ...] = ()
    estimator_version: str = "phase9.2-median-v1"

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("cost history snapshot_id is required")
        ids = [sample.action_id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("cost history snapshot has duplicate action ids")

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "samples": [sample.to_dict() for sample in self.samples],
            "ledger_event_ids": list(self.ledger_event_ids),
            "estimator_version": self.estimator_version,
        }
    @classmethod
    def from_completed_ledger(
        cls,
        ledger: CostLedger,
        *,
        snapshot_id: str,
        buckets_by_action_id: Mapping[str, CostBucket],
        estimator_version: str = "phase9.2-median-v1",
    ) -> "CostHistorySnapshot":
        """Extract samples only where every recorded event is completed.

        Ledger writers associate events with actions through
        ``metadata["action_id"]``.  A failed or in-progress event excludes that
        action entirely, preventing partial/future observations from leaking
        into policy estimates.
        """
        grouped: dict[str, list[CostLedgerEvent]] = {}
        for event in ledger.events:
            action_id = event.metadata.get("action_id")
            if isinstance(action_id, str) and action_id in buckets_by_action_id:
                grouped.setdefault(action_id, []).append(event)
        samples: list[CompletedActionCost] = []
        consumed: list[str] = []
        for action_id in sorted(grouped):
            events = grouped[action_id]
            if not events or any(event.status != "completed" for event in events):
                continue
            samples.append(CompletedActionCost(
                action_id=action_id,
                bucket=buckets_by_action_id[action_id],
                actual=_actual_from_events(events, estimator_version),
                completed_event_ids=tuple(event.event_id for event in events),
            ))
            consumed.extend(event.event_id for event in events)
        return cls(
            snapshot_id=snapshot_id,
            samples=tuple(samples),
            ledger_event_ids=tuple(consumed),
            estimator_version=estimator_version,
        )


def cost_history_snapshot_fingerprint(snapshot: CostHistorySnapshot) -> str:
    """Stable SHA-256 over the snapshot's canonical serialized content."""
    # Round-trip through the public decoder so semantically identical numeric
    # values (for example 1 and 1.0) have one canonical representation.
    canonical = cost_history_snapshot_from_dict(snapshot.to_dict()).to_dict()
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _estimate_from_dict(data: Mapping[str, object]) -> CostEstimate:
    def item(name: str) -> Estimate | None:
        raw = data.get(name)
        if not isinstance(raw, Mapping):
            return None
        return Estimate(float(raw["value"]))
    return CostEstimate(
        **{name: item(name) for name in _DIMENSIONS},
        sample_count=int(data.get("sample_count", 0)), source=str(data.get("source", "unavailable")),
        estimator_version=str(data.get("estimator_version", "phase9.2-median-v1")),
    )


def cost_history_snapshot_from_dict(data: Mapping[str, object]) -> CostHistorySnapshot:
    raw_samples = data.get("samples", ())
    if not isinstance(raw_samples, list):
        raise ValueError("cost history snapshot samples must be a list")
    samples: list[CompletedActionCost] = []
    for raw in raw_samples:
        if not isinstance(raw, Mapping):
            raise ValueError("cost history sample must be a dictionary")
        bucket = raw.get("bucket")
        actual = raw.get("actual")
        if not isinstance(bucket, Mapping) or not isinstance(actual, Mapping):
            raise ValueError("cost history sample bucket and actual must be dictionaries")
        completed_ids = raw.get("completed_event_ids", ())
        if not isinstance(completed_ids, list):
            raise ValueError("cost history completed_event_ids must be a list")
        samples.append(CompletedActionCost(
            action_id=str(raw["action_id"]), bucket=cost_bucket_from_dict(bucket),
            actual=_estimate_from_dict(actual),
            completed_event_ids=tuple(str(item) for item in completed_ids),
        ))
    ledger_ids = data.get("ledger_event_ids", ())
    if not isinstance(ledger_ids, list):
        raise ValueError("cost history ledger_event_ids must be a list")
    return CostHistorySnapshot(
        snapshot_id=str(data["snapshot_id"]), samples=tuple(samples),
        ledger_event_ids=tuple(str(item) for item in ledger_ids),
        estimator_version=str(data.get("estimator_version", "phase9.2-median-v1")),
    )


@dataclass(frozen=True)
class CostEstimation:
    """Estimate plus traceable fallback/coverage information."""

    estimate: CostEstimate
    bucket: CostBucket
    fallback_reason: str | None
    matching_sample_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "estimate": self.estimate.to_dict(), "bucket": self.bucket.to_dict(),
            "fallback_reason": self.fallback_reason,
            "matching_sample_count": self.matching_sample_count,
        }


@dataclass(frozen=True)
class CalibrationReport:
    sample_count: int
    coverage: Mapping[str, float]
    median_absolute_error: Mapping[str, float | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "coverage": dict(self.coverage),
            "median_absolute_error": dict(self.median_absolute_error),
        }


class ActionCostEstimator:
    """Median estimator over a pre-frozen history snapshot."""

    def __init__(
        self,
        snapshot: CostHistorySnapshot,
        *,
        min_samples: int = 3,
        priors: Mapping[SearchActionKind, CostEstimate] | None = None,
    ) -> None:
        if min_samples < 1:
            raise ValueError("min_samples must be positive")
        self.snapshot = snapshot
        self.min_samples = min_samples
        self.priors = dict(priors or {})

    def estimate(self, bucket: CostBucket) -> CostEstimation:
        matching = tuple(sample for sample in self.snapshot.samples if sample.bucket == bucket)
        if len(matching) >= self.min_samples:
            return CostEstimation(
                estimate=_median_estimate(matching, self.snapshot.estimator_version),
                bucket=bucket, fallback_reason=None, matching_sample_count=len(matching),
            )
        prior = self.priors.get(bucket.action_kind, static_execution_cost(bucket.action_kind))
        return CostEstimation(
            estimate=replace(
                prior, sample_count=len(matching), source="prior",
                estimator_version=self.snapshot.estimator_version,
            ),
            bucket=bucket,
            fallback_reason=("cold_start" if not matching else "insufficient_matching_samples"),
            matching_sample_count=len(matching),
        )

    def calibration_report(self, samples: Iterable[CompletedActionCost]) -> CalibrationReport:
        """Evaluate estimates against a caller-provided held-out sample set."""
        held_out = tuple(samples)
        errors: dict[str, list[float]] = {dimension: [] for dimension in _DIMENSIONS}
        present: dict[str, int] = {dimension: 0 for dimension in _DIMENSIONS}
        for sample in held_out:
            predicted = self.estimate(sample.bucket).estimate
            for dimension in _DIMENSIONS:
                actual_value = _value(sample.actual, dimension)
                predicted_value = _value(predicted, dimension)
                if actual_value is not None:
                    present[dimension] += 1
                # A missing dimension cannot be used in a comparison.
                if actual_value is not None and predicted_value is not None:
                    errors[dimension].append(abs(actual_value - predicted_value))
        total = len(held_out)
        return CalibrationReport(
            sample_count=total,
            coverage={key: (present[key] / total if total else 0.0) for key in _DIMENSIONS},
            median_absolute_error={key: (median(values) if values else None) for key, values in errors.items()},
        )


def _actual_from_events(events: Iterable[CostLedgerEvent], estimator_version: str) -> CostEstimate:
    events = tuple(events)
    provider_requests = [event for event in events if event.kind is CostLedgerEventKind.PROVIDER_REQUEST]
    provider_usage = [event for event in events if event.kind is CostLedgerEventKind.PROVIDER_USAGE]
    checks = [event for event in events if event.kind is CostLedgerEventKind.CHECKER]
    charges = [event for event in events if event.kind is CostLedgerEventKind.CHARGE]
    return CostEstimate(
        model_requests=Estimate(float(len(provider_requests))),
        input_tokens=_sum_event_measurements(
            provider_usage, "input_tokens", zero_when_absent=not provider_requests
        ),
        output_tokens=_sum_event_measurements(
            provider_usage, "output_tokens", zero_when_absent=not provider_requests
        ),
        billed_tokens=_sum_event_measurements(
            provider_usage, "billed_tokens", zero_when_absent=not provider_requests
        ),
        checks=Estimate(float(len(checks))),
        checker_wall_ms=_sum_event_measurements(checks, "wall_time_ms"),
        checker_cpu_ms=_sum_event_measurements(checks, "cpu_time_ms"),
        api_cost_usd=_sum_event_measurements(charges, "api_cost_usd"),
        sample_count=1, source="history", estimator_version=estimator_version,
    )


def actual_cost_from_events(
    events: Iterable[CostLedgerEvent],
    *,
    estimator_version: str = "phase9.2-actual-v1",
) -> CostEstimate:
    """Project completed runtime events into the public action-cost vector."""
    return _actual_from_events(events, estimator_version)


def estimate_error(
    estimate: CostEstimate,
    actual: CostEstimate,
) -> dict[str, float | None]:
    """Return signed estimate-minus-actual errors without inventing NA values."""
    return {
        dimension: (
            _value(estimate, dimension) - _value(actual, dimension)
            if _value(estimate, dimension) is not None
            and _value(actual, dimension) is not None
            else None
        )
        for dimension in _DIMENSIONS
    }


def _sum_event_measurements(
    events: Iterable[CostLedgerEvent],
    field: str,
    *,
    zero_when_absent: bool = True,
) -> Estimate | None:
    events = tuple(events)
    if not events:
        # No provider event means this controlled action used zero tokens; this
        # is deliberately different from a provider response missing usage.
        return Estimate(0.0) if zero_when_absent else None
    values: list[float] = []
    for event in events:
        measurement = getattr(event, field)
        if measurement is None or measurement.status not in {MeasurementStatus.OBSERVED, MeasurementStatus.ESTIMATED}:
            return None
        assert measurement.value is not None
        values.append(float(measurement.value))
    return Estimate(sum(values))


def _median_estimate(samples: Iterable[CompletedActionCost], estimator_version: str) -> CostEstimate:
    samples = tuple(samples)
    values: dict[str, Estimate | None] = {}
    for dimension in _DIMENSIONS:
        observed = [_value(sample.actual, dimension) for sample in samples]
        usable = [value for value in observed if value is not None]
        values[dimension] = Estimate(float(median(usable))) if usable else None
    return CostEstimate(
        **values, sample_count=len(samples), source="history", estimator_version=estimator_version,
    )


def _value(estimate: CostEstimate, dimension: str) -> float | None:
    item = getattr(estimate, dimension)
    return item.value if item is not None else None
