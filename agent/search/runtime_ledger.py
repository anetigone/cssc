"""Controller-neutral runtime cost event writers.

Both minimal and structured execution use these helpers so benchmark arms
observe provider and checker cost with identical missing-value semantics.
"""

from __future__ import annotations

from typing import Any, Mapping

from .cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)


def record_generation_events(
    ledger: CostLedger,
    *,
    metadata: Mapping[str, Any],
    attempt_index: int,
    fallback_request_id: str,
    status: str,
) -> CostLedger:
    """Record physical provider attempts, usage, tools, and optional charges."""
    usage = metadata.get("token_usage")
    model = metadata.get("model")
    attempts = metadata.get("provider_requests")
    is_provider = isinstance(model, str) or isinstance(usage, Mapping) or isinstance(attempts, (list, tuple))
    if not is_provider:
        return ledger

    physical = attempts if isinstance(attempts, (list, tuple)) and attempts else ({
        "request_id": fallback_request_id,
        "status": status,
        "token_usage": usage,
    },)
    for raw in physical:
        if not isinstance(raw, Mapping):
            continue
        request_id = str(raw.get("request_id") or fallback_request_id)
        request_status = str(raw.get("status", status))
        request_usage = raw.get("token_usage")
        request_usage = request_usage if isinstance(request_usage, Mapping) else None
        retry_scope = CostScope.RETRY if request_status == "retry" else CostScope.PROPOSAL_GENERATION
        ledger = ledger.append(CostLedgerEvent(
            event_id=f"provider-request:{len(ledger.events)}",
            kind=CostLedgerEventKind.PROVIDER_REQUEST,
            scope=retry_scope,
            status=request_status,
            attempt_index=attempt_index,
            request_id=request_id,
            model=model if isinstance(model, str) else None,
            wall_time_ms=_measurement(raw.get("wall_time_ms"), "provider wall time not reported"),
            metadata={"retry_index": raw.get("retry_index"), "http_status": raw.get("http_status"), "transport_error": raw.get("error")},
        ))
        ledger = ledger.append(CostLedgerEvent(
            event_id=f"provider-usage:{len(ledger.events)}",
            kind=CostLedgerEventKind.PROVIDER_USAGE,
            scope=retry_scope,
            status=request_status,
            attempt_index=attempt_index,
            request_id=request_id,
            model=model if isinstance(model, str) else None,
            input_tokens=_usage_measurement(request_usage, "input_tokens"),
            output_tokens=_usage_measurement(request_usage, "output_tokens"),
            reasoning_tokens=_usage_measurement(request_usage, "reasoning_tokens"),
            cached_tokens=_usage_measurement(request_usage, "cached_tokens"),
            billed_tokens=_usage_measurement(request_usage, "provider_total_tokens"),
            usage_source="provider_response" if request_usage is not None else "provider_usage_unavailable",
        ))

    for raw in metadata.get("tool_calls", ()) if isinstance(metadata.get("tool_calls"), (list, tuple)) else ():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("tool_kind"), str):
            continue
        ledger = ledger.append(CostLedgerEvent(
            event_id=f"tool:{len(ledger.events)}",
            kind=CostLedgerEventKind.TOOL_CALL,
            scope=CostScope.TOOL_CHECK,
            status=str(raw.get("status", status)),
            attempt_index=attempt_index,
            call_id=str(raw.get("call_id") or f"tool:{len(ledger.events)}"),
            tool_kind=str(raw["tool_kind"]),
            wall_time_ms=_measurement(raw.get("wall_time_ms"), "tool wall time not reported"),
        ))

    pricing = metadata.get("pricing")
    if isinstance(pricing, Mapping) and all(
        isinstance(pricing.get(key), str) and pricing.get(key)
        for key in ("currency", "price_table_version", "effective_date")
    ):
        ledger = ledger.append(CostLedgerEvent(
            event_id=f"pricing:{len(ledger.events)}",
            kind=CostLedgerEventKind.PRICING,
            scope=CostScope.PROPOSAL_GENERATION,
            status=status,
            attempt_index=attempt_index,
            request_id=fallback_request_id,
            currency=str(pricing["currency"]),
            price_table_version=str(pricing["price_table_version"]),
            effective_date=str(pricing["effective_date"]),
            metadata={"rates_per_million_usd": dict(pricing.get("rates_per_million_usd") or {})},
        ))
    charge = metadata.get("api_cost_usd")
    estimated_charge = estimated_api_charge(usage, pricing)
    reported = isinstance(charge, (int, float)) and not isinstance(charge, bool)
    ledger = ledger.append(CostLedgerEvent(
        event_id=f"charge:{len(ledger.events)}",
        kind=CostLedgerEventKind.CHARGE,
        scope=CostScope.PROPOSAL_GENERATION,
        status=status,
        attempt_index=attempt_index,
        request_id=fallback_request_id,
        api_cost_usd=(
            CostMeasurement.observed(charge)
            if reported
            else CostMeasurement.estimated(estimated_charge)
            if estimated_charge is not None
            else CostMeasurement.unavailable("provider did not report API cost and no complete frozen price table was supplied")
        ),
        estimation_method="provider_reported" if reported else "frozen_price_table" if estimated_charge is not None else None,
    ))
    return ledger


def record_checker_event(ledger: CostLedger, *, attempt_index: int, check_result: Any, checker_kind: str = "candidate") -> CostLedger:
    return ledger.append(CostLedgerEvent(
        event_id=f"checker:{len(ledger.events)}",
        kind=CostLedgerEventKind.CHECKER,
        scope=CostScope.TOOL_CHECK,
        status="completed",
        attempt_index=attempt_index,
        checker_kind=checker_kind,
        category=check_result.category.value,
        wall_time_ms=CostMeasurement.observed(float(check_result.elapsed_seconds) * 1000),
        cpu_time_ms=CostMeasurement.unavailable("checker CPU time not reported"),
    ))


def _measurement(value: Any, reason: str) -> CostMeasurement:
    return CostMeasurement.observed(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else CostMeasurement.unavailable(reason)


def _usage_measurement(usage: Mapping[str, Any] | None, key: str) -> CostMeasurement:
    value = usage.get(key) if usage is not None else None
    return _measurement(value, f"provider omitted {key}")


def estimated_api_charge(usage: Any, pricing: Any) -> float | None:
    if not isinstance(usage, Mapping) or not isinstance(pricing, Mapping):
        return None
    rates = pricing.get("rates_per_million_usd")
    if not isinstance(rates, Mapping):
        return None
    required_usage = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
    if any(not isinstance(usage.get(key), (int, float)) for key in required_usage):
        return None
    required_rates = ("input", "cached_input", "output", "reasoning")
    if any(not isinstance(rates.get(key), (int, float)) for key in required_rates):
        return None
    cached = min(float(usage["cached_tokens"]), float(usage["input_tokens"]))
    uncached = float(usage["input_tokens"]) - cached
    total = (
        uncached * float(rates["input"])
        + cached * float(rates["cached_input"])
        + float(usage["output_tokens"]) * float(rates["output"])
        + float(usage["reasoning_tokens"]) * float(rates["reasoning"])
    )
    return total / 1_000_000
