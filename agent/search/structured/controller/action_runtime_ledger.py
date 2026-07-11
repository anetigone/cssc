"""Phase 9 ledger and budget helpers for the action runtime.

Kept separate from the scheduling loop so cost-accounting details do not make
the controller's control flow difficult to audit.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from agent.search.budget import BudgetManager
from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)

from ..budget_snapshot import ActionBudgetLimits, build_unified_budget_snapshot


def unified_budget_snapshot(budget: BudgetManager, state):
    """Build the live Phase 9 budget view from the shared runtime config."""
    config = budget.config
    return build_unified_budget_snapshot(
        budget.snapshot(),
        state.cost_ledger,
        limits=ActionBudgetLimits(
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            max_billed_tokens=config.max_billed_tokens,
            max_elapsed_seconds=config.max_elapsed_seconds,
            max_api_cost_usd=config.max_api_cost_usd,
            global_reserve_checks=config.global_reserve_checks,
            global_reserve_model_requests=config.global_reserve_model_requests,
        ),
    )


def record_proposal_request(
    state,
    request_id: str,
    branch,
    elapsed: float,
    *,
    status: str,
    proposals,
    error: str | None = None,
    provider_used: bool = False,
    route_decision=None,
    failure_metadata: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Append every provider outcome, preserving unavailable usage as NA."""
    metadata = proposals[0].metadata if proposals else {}
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    if failure_metadata:
        metadata = {**failure_metadata, **metadata}
    usage = metadata.get("token_usage")
    model = metadata.get("model")
    if not isinstance(model, str) and route_decision is not None:
        model = route_decision.model
    is_provider = provider_used or isinstance(model, str) or isinstance(usage, dict)
    if not is_provider:
        return "deterministic", None

    tier = metadata.get(
        "model_tier", getattr(getattr(route_decision, "tier", None), "value", "cheap")
    )
    common = {
        "action_id": None,
        "branch_id": branch.branch_id,
        "obligation_id": branch.obligation_id,
        "error": error,
        "routing": metadata.get("routing"),
    }
    state.cost_ledger = state.cost_ledger.append(CostLedgerEvent(
        event_id=f"provider-request:{len(state.cost_ledger.events)}",
        kind=CostLedgerEventKind.PROVIDER_REQUEST,
        scope=CostScope.PROPOSAL_GENERATION,
        status=status,
        attempt_index=state.attempt_index,
        request_id=request_id,
        model=model,
        model_tier=str(tier),
        wall_time_ms=CostMeasurement.observed(elapsed * 1000),
        metadata=common,
    ))

    def measured(name: str) -> CostMeasurement:
        value = usage.get(name) if isinstance(usage, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return CostMeasurement.observed(value)
        return CostMeasurement.unavailable(f"provider omitted {name}")

    state.cost_ledger = state.cost_ledger.append(CostLedgerEvent(
        event_id=f"provider-usage:{len(state.cost_ledger.events)}",
        kind=CostLedgerEventKind.PROVIDER_USAGE,
        scope=CostScope.PROPOSAL_GENERATION,
        status=status,
        attempt_index=state.attempt_index,
        request_id=request_id,
        model=model,
        model_tier=str(tier),
        input_tokens=measured("input_tokens"),
        output_tokens=measured("output_tokens"),
        reasoning_tokens=measured("reasoning_tokens"),
        cached_tokens=measured("cached_tokens"),
        billed_tokens=measured("provider_total_tokens"),
        usage_source="provider_response" if isinstance(usage, dict) else "provider_usage_unavailable",
        metadata=common,
    ))
    _record_charges(state, request_id, status, metadata, common)
    _record_tools(state, branch, status, metadata, common)
    if isinstance(usage, dict):
        state.model_usage.append(dict(usage))
    return "model", str(tier)


def _record_charges(state, request_id: str, status: str, metadata, common) -> None:
    pricing = metadata.get("pricing")
    if isinstance(pricing, dict):
        currency, version, effective_date = (
            pricing.get("currency"), pricing.get("price_table_version"), pricing.get("effective_date")
        )
        if all(isinstance(item, str) and item for item in (currency, version, effective_date)):
            unit_price = pricing.get("unit_price")
            state.cost_ledger = state.cost_ledger.append(CostLedgerEvent(
                event_id=f"pricing:{len(state.cost_ledger.events)}",
                kind=CostLedgerEventKind.PRICING, scope=CostScope.PROPOSAL_GENERATION,
                status=status, attempt_index=state.attempt_index, request_id=request_id,
                currency=currency, price_table_version=version, effective_date=effective_date,
                unit_price=(CostMeasurement.observed(unit_price) if isinstance(unit_price, (int, float)) else CostMeasurement.unavailable("price table omitted unit_price")),
                metadata=common,
            ))
    charge = metadata.get("api_cost_usd")
    reported = isinstance(charge, (int, float)) and not isinstance(charge, bool)
    state.cost_ledger = state.cost_ledger.append(CostLedgerEvent(
        event_id=f"charge:{len(state.cost_ledger.events)}",
        kind=CostLedgerEventKind.CHARGE, scope=CostScope.PROPOSAL_GENERATION,
        status=status, attempt_index=state.attempt_index, request_id=request_id,
        api_cost_usd=(CostMeasurement.observed(charge) if reported else CostMeasurement.unavailable("provider did not report API cost")),
        estimation_method="provider_reported" if reported else None,
        metadata=common,
    ))


def _record_tools(state, branch, status: str, metadata, common) -> None:
    calls = metadata.get("tool_calls")
    if not isinstance(calls, (list, tuple)):
        return
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        call_id = call.get("call_id") or f"tool:{branch.branch_id}:{index}"
        kind = call.get("tool_kind")
        if not isinstance(call_id, str) or not isinstance(kind, str):
            continue
        elapsed = call.get("wall_time_ms")
        state.cost_ledger = state.cost_ledger.append(CostLedgerEvent(
            event_id=f"tool:{len(state.cost_ledger.events)}",
            kind=CostLedgerEventKind.TOOL_CALL, scope=CostScope.TOOL_CHECK,
            status=str(call.get("status", status)), attempt_index=state.attempt_index,
            call_id=call_id, tool_kind=kind,
            wall_time_ms=(CostMeasurement.observed(elapsed) if isinstance(elapsed, (int, float)) else CostMeasurement.unavailable("tool wall time not reported")),
            metadata=common,
        ))


def attribute_proposal_batch(state, proposal, node_id: str) -> None:
    """Attribute a consumed proposal batch to one action without new charges."""
    batch_id = proposal.metadata.get("proposal_batch_id")
    if not isinstance(batch_id, str):
        return
    events = tuple(
        replace(event, metadata={**event.metadata, "action_id": node_id})
        if event.request_id == batch_id else event
        for event in state.cost_ledger.events
    )
    state.cost_ledger = CostLedger(events)
