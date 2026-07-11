"""Append-only, event-level cost accounting for proof-search runs.

The older :mod:`agent.search.cost` projection deliberately treats unavailable
provider usage as zero.  That remains useful for backwards-compatible Phase 8
views, but is not suitable for spending decisions.  This module is the Phase
9 source of truth: every measured quantity carries an explicit status, so an
unavailable measurement can never silently become a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class MeasurementStatus(str, Enum):
    """Whether a cost measurement is known, estimated, unavailable, or unbounded."""

    OBSERVED = "observed"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"
    UNBOUNDED = "unbounded"


@dataclass(frozen=True)
class CostMeasurement:
    """One numeric measurement with non-ambiguous missing-value semantics.

    ``value=0`` is an observed/estimated zero.  ``None`` is only valid for
    unavailable and unbounded measurements, which must state why.  In
    particular, a provider response with no ``usage`` payload is represented
    as ``unavailable``, never as zero tokens.
    """

    value: int | float | None
    status: MeasurementStatus
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status in {MeasurementStatus.OBSERVED, MeasurementStatus.ESTIMATED}:
            if self.value is None:
                raise ValueError("observed or estimated measurements require a value")
            if self.reason is not None:
                raise ValueError("observed or estimated measurements cannot carry a reason")
        elif self.value is not None:
            raise ValueError("unavailable or unbounded measurements must have value=None")
        elif not self.reason:
            raise ValueError("unavailable or unbounded measurements require a reason")
        if isinstance(self.value, bool) or (
            self.value is not None and not isinstance(self.value, (int, float))
        ):
            raise TypeError("measurement value must be numeric or None")
        if self.value is not None and self.value < 0:
            raise ValueError("measurement value cannot be negative")

    @classmethod
    def observed(cls, value: int | float) -> "CostMeasurement":
        return cls(value=value, status=MeasurementStatus.OBSERVED)

    @classmethod
    def estimated(cls, value: int | float) -> "CostMeasurement":
        return cls(value=value, status=MeasurementStatus.ESTIMATED)

    @classmethod
    def unavailable(cls, reason: str) -> "CostMeasurement":
        return cls(value=None, status=MeasurementStatus.UNAVAILABLE, reason=reason)

    @classmethod
    def unbounded(cls, reason: str = "no configured limit") -> "CostMeasurement":
        return cls(value=None, status=MeasurementStatus.UNBOUNDED, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "measurement_status": self.status.value, "reason": self.reason}


def measurement_from_dict(data: Mapping[str, Any] | None, *, missing_reason: str) -> CostMeasurement:
    """Decode a measurement, treating legacy absent fields as unavailable."""
    if data is None:
        return CostMeasurement.unavailable(missing_reason)
    raw_status = data.get("measurement_status", data.get("status"))
    if raw_status is None:
        return CostMeasurement.unavailable(missing_reason)
    try:
        status = MeasurementStatus(str(raw_status))
    except ValueError as exc:
        raise ValueError(f"unknown measurement status: {raw_status!r}") from exc
    return CostMeasurement(data.get("value"), status, data.get("reason"))


class CostLedgerEventKind(str, Enum):
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_USAGE = "provider_usage"
    TOOL_CALL = "tool_call"
    CHECKER = "checker"
    PRICING = "pricing"
    CHARGE = "charge"


class CostScope(str, Enum):
    PROPOSAL_GENERATION = "proposal_generation"
    EXECUTION = "execution"
    TOOL_CHECK = "tool_check"
    ASSEMBLY = "assembly"
    RETRY = "retry"


@dataclass(frozen=True)
class CostLedgerEvent:
    """An immutable event emitted for one provider/tool/checker cost fact.

    Fields not relevant to an event kind are ``None``.  Quantities always use
    :class:`CostMeasurement`; event writers must therefore make missing data
    explicit instead of relying on dict defaults.
    """

    event_id: str
    kind: CostLedgerEventKind
    scope: CostScope
    status: str
    attempt_index: int | None = None
    request_id: str | None = None
    call_id: str | None = None
    model: str | None = None
    model_tier: str | None = None
    tool_kind: str | None = None
    checker_kind: str | None = None
    category: str | None = None
    wall_time_ms: CostMeasurement | None = None
    cpu_time_ms: CostMeasurement | None = None
    input_tokens: CostMeasurement | None = None
    output_tokens: CostMeasurement | None = None
    reasoning_tokens: CostMeasurement | None = None
    cached_tokens: CostMeasurement | None = None
    billed_tokens: CostMeasurement | None = None
    api_cost_usd: CostMeasurement | None = None
    usage_source: str | None = None
    currency: str | None = None
    unit_price: CostMeasurement | None = None
    price_table_version: str | None = None
    effective_date: str | None = None
    estimation_method: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("cost ledger event_id is required")
        if not isinstance(self.kind, CostLedgerEventKind):
            raise TypeError("cost ledger event kind must be CostLedgerEventKind")
        if not isinstance(self.scope, CostScope):
            raise TypeError("cost ledger event scope must be CostScope")
        if self.attempt_index is not None and self.attempt_index < 0:
            raise ValueError("attempt_index cannot be negative")
        if self.kind in {CostLedgerEventKind.PROVIDER_REQUEST, CostLedgerEventKind.PROVIDER_USAGE} and not self.request_id:
            raise ValueError(f"{self.kind.value} events require request_id")
        if self.kind is CostLedgerEventKind.TOOL_CALL and not self.call_id:
            raise ValueError("tool_call events require call_id")
        if self.kind is CostLedgerEventKind.CHECKER and not self.checker_kind:
            raise ValueError("checker events require checker_kind")
        if self.kind is CostLedgerEventKind.PRICING:
            if not (self.currency and self.price_table_version and self.effective_date):
                raise ValueError("pricing events require currency, price table version, and effective date")
        if self.kind is CostLedgerEventKind.CHARGE and self.api_cost_usd is None:
            raise ValueError("charge events require api_cost_usd")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": self.event_id, "kind": self.kind.value, "scope": self.scope.value,
            "status": self.status, "attempt_index": self.attempt_index, "request_id": self.request_id,
            "call_id": self.call_id, "model": self.model, "model_tier": self.model_tier,
            "tool_kind": self.tool_kind, "checker_kind": self.checker_kind, "category": self.category,
            "usage_source": self.usage_source, "currency": self.currency, "unit_price": _measurement_payload(self.unit_price),
            "price_table_version": self.price_table_version, "effective_date": self.effective_date,
            "estimation_method": self.estimation_method, "metadata": dict(self.metadata),
        }
        for name in _MEASUREMENT_FIELDS:
            payload[name] = _measurement_payload(getattr(self, name))
        return payload


_MEASUREMENT_FIELDS = (
    "wall_time_ms", "cpu_time_ms", "input_tokens", "output_tokens", "reasoning_tokens",
    "cached_tokens", "billed_tokens", "api_cost_usd",
)


def _measurement_payload(value: CostMeasurement | None) -> dict[str, Any] | None:
    return value.to_dict() if value is not None else None


def cost_ledger_event_from_dict(data: Mapping[str, Any]) -> CostLedgerEvent:
    """Decode an event. Missing quantities stay missing rather than becoming zero."""
    try:
        kind = CostLedgerEventKind(str(data["kind"]))
        scope = CostScope(str(data["scope"]))
    except KeyError as exc:
        raise ValueError(f"cost ledger event missing required field: {exc.args[0]}") from exc
    kwargs = {
        name: measurement_from_dict(data.get(name), missing_reason=f"{name} absent from event")
        if data.get(name) is not None else None
        for name in _MEASUREMENT_FIELDS
    }
    unit_price = data.get("unit_price")
    return CostLedgerEvent(
        event_id=str(data["event_id"]), kind=kind, scope=scope, status=str(data.get("status", "unknown")),
        attempt_index=data.get("attempt_index"), request_id=data.get("request_id"), call_id=data.get("call_id"),
        model=data.get("model"), model_tier=data.get("model_tier"), tool_kind=data.get("tool_kind"),
        checker_kind=data.get("checker_kind"), category=data.get("category"), usage_source=data.get("usage_source"),
        currency=data.get("currency"), unit_price=(measurement_from_dict(unit_price, missing_reason="unit_price absent from event") if unit_price is not None else None),
        price_table_version=data.get("price_table_version"), effective_date=data.get("effective_date"),
        estimation_method=data.get("estimation_method"), metadata=dict(data.get("metadata") or {}), **kwargs,
    )


@dataclass(frozen=True)
class LedgerTotal:
    """A sum that remains unavailable if any contributing item is unavailable."""

    measurement: CostMeasurement
    contributing_event_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**self.measurement.to_dict(), "contributing_event_ids": list(self.contributing_event_ids)}


@dataclass(frozen=True)
class CostLedgerReconciliation:
    """Reconciled run totals plus an explicit record of incomplete dimensions."""

    totals: Mapping[str, LedgerTotal]
    scope_event_counts: Mapping[str, int]
    unallocated_event_ids: tuple[str, ...]

    @property
    def reconciled(self) -> bool:
        return not self.unallocated_event_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "totals": {key: value.to_dict() for key, value in self.totals.items()},
            "scope_event_counts": dict(self.scope_event_counts),
            "unallocated_event_ids": list(self.unallocated_event_ids),
        }


@dataclass(frozen=True)
class CostLedger:
    """Append-only event collection. ``append`` always returns a new ledger."""

    events: tuple[CostLedgerEvent, ...] = ()

    def __post_init__(self) -> None:
        ids = [event.event_id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("cost ledger event ids must be unique")

    def append(self, event: CostLedgerEvent) -> "CostLedger":
        if any(existing.event_id == event.event_id for existing in self.events):
            raise ValueError(f"duplicate cost ledger event id: {event.event_id}")
        return CostLedger(self.events + (event,))

    def extend(self, events: Iterable[CostLedgerEvent]) -> "CostLedger":
        ledger = self
        for event in events:
            ledger = ledger.append(event)
        return ledger

    def reconcile(self) -> CostLedgerReconciliation:
        """Return non-double-counted totals over the canonical event kinds.

        Tokens come only from ``provider_usage`` events, check timings only
        from ``checker`` events, and API cost only from ``charge`` events.
        Thus provider requests and price-table rows contribute provenance but
        never inflate run totals.
        """
        accounted_events: list[CostLedgerEvent] = []
        duplicate_event_ids: list[str] = []
        seen_usage_requests: set[str] = set()
        for event in self.events:
            if event.kind is CostLedgerEventKind.PROVIDER_USAGE:
                assert event.request_id is not None
                if event.request_id in seen_usage_requests:
                    duplicate_event_ids.append(event.event_id)
                    continue
                seen_usage_requests.add(event.request_id)
            accounted_events.append(event)

        dimensions = {
            "input_tokens": (CostLedgerEventKind.PROVIDER_USAGE,),
            "output_tokens": (CostLedgerEventKind.PROVIDER_USAGE,),
            "reasoning_tokens": (CostLedgerEventKind.PROVIDER_USAGE,),
            "cached_tokens": (CostLedgerEventKind.PROVIDER_USAGE,),
            "billed_tokens": (CostLedgerEventKind.PROVIDER_USAGE,),
            "checker_wall_time_ms": (CostLedgerEventKind.CHECKER,),
            "checker_cpu_time_ms": (CostLedgerEventKind.CHECKER,),
            "api_cost_usd": (CostLedgerEventKind.CHARGE,),
        }
        totals = {
            name: _sum_measurements(
                ((event.event_id, getattr(event, field)) for event in accounted_events if event.kind in kinds),
                missing_reason=f"no {name} measurements recorded",
            )
            for name, (kinds, field) in {
                **{name: (kinds, name) for name, kinds in dimensions.items() if name not in {"checker_wall_time_ms", "checker_cpu_time_ms"}},
                "checker_wall_time_ms": ((CostLedgerEventKind.CHECKER,), "wall_time_ms"),
                "checker_cpu_time_ms": ((CostLedgerEventKind.CHECKER,), "cpu_time_ms"),
            }.items()
        }
        counts = {scope.value: sum(event.scope is scope for event in accounted_events) for scope in CostScope}
        unallocated = tuple(duplicate_event_ids)
        return CostLedgerReconciliation(totals=totals, scope_event_counts=counts, unallocated_event_ids=unallocated)

    def to_dict(self) -> dict[str, Any]:
        return {"events": [event.to_dict() for event in self.events], "reconciliation": self.reconcile().to_dict()}


def _sum_measurements(
    entries: Iterable[tuple[str, CostMeasurement | None]], *, missing_reason: str
) -> LedgerTotal:
    entries = tuple(entries)
    ids = tuple(event_id for event_id, _ in entries)
    if not entries:
        return LedgerTotal(CostMeasurement.unavailable(missing_reason), ids)
    measurements = [measurement for _, measurement in entries]
    if any(measurement is None for measurement in measurements):
        return LedgerTotal(CostMeasurement.unavailable("measurement missing from one or more events"), ids)
    typed = [measurement for measurement in measurements if measurement is not None]
    unavailable = next((item for item in typed if item.status is MeasurementStatus.UNAVAILABLE), None)
    if unavailable is not None:
        return LedgerTotal(CostMeasurement.unavailable(unavailable.reason or "measurement unavailable"), ids)
    unbounded = next((item for item in typed if item.status is MeasurementStatus.UNBOUNDED), None)
    if unbounded is not None:
        return LedgerTotal(CostMeasurement.unbounded(unbounded.reason or "measurement unbounded"), ids)
    status = MeasurementStatus.ESTIMATED if any(item.status is MeasurementStatus.ESTIMATED for item in typed) else MeasurementStatus.OBSERVED
    return LedgerTotal(CostMeasurement(sum(item.value or 0 for item in typed), status), ids)


def cost_ledger_from_dict(data: Mapping[str, Any] | None) -> CostLedger:
    """Read a ledger snapshot; absent legacy trace data becomes an empty ledger."""
    if not data:
        return CostLedger()
    raw_events = data.get("events", ())
    if not isinstance(raw_events, Iterable) or isinstance(raw_events, (str, bytes, Mapping)):
        raise ValueError("cost ledger events must be a sequence")
    return CostLedger(tuple(cost_ledger_event_from_dict(event) for event in raw_events))
