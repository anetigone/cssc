"""Aggregate miniF2F outcomes and token usage across resumed run traces."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .minif2f import MiniF2FError
from .minif2f_run_report import (
    execution_mode_from_proof_args,
    saved_result_is_infrastructure,
)


TOKEN_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "reasoning_tokens",
    "provider_completion_tokens",
    "provider_total_tokens",
)
LEDGER_FIELDS = (*TOKEN_FIELDS[:4], "billed_tokens", "api_cost_usd")


def build_minif2f_usage_report(run_root: str | Path) -> dict[str, Any]:
    """Build a report without modifying the append-only benchmark evidence."""
    root = Path(run_root).resolve()
    run_metadata = _read_json_object(root / "run.json")
    summary = _read_json_object(root / "summary.json")
    task_ids = run_metadata.get("task_ids")
    if not isinstance(task_ids, list) or not all(
        isinstance(task_id, str) for task_id in task_ids
    ):
        raise MiniF2FError("run.json does not contain a valid task_ids list")

    result_payloads: dict[str, dict[str, Any]] = {}
    sessions: list[dict[str, Any]] = []
    for task_id in task_ids:
        result_path = root / "tasks" / task_id / "result.json"
        if result_path.is_file():
            result_payloads[task_id] = _read_json_object(result_path)
        trace_path = root / "tasks" / task_id / "trace.jsonl"
        if trace_path.is_file():
            sessions.extend(_trace_sessions(trace_path, task_id))

    sessions_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        sessions_by_task[session["task_id"]].append(session)

    current_accepted_sessions: list[dict[str, Any]] = []
    accepted_without_trace: list[str] = []
    for task_id in task_ids:
        result = result_payloads.get(task_id)
        if not result or not result.get("ok"):
            continue
        task_sessions = sessions_by_task.get(task_id, ())
        if not task_sessions:
            accepted_without_trace.append(task_id)
            continue
        current_accepted_sessions.append(task_sessions[-1])

    first_sessions = [
        task_sessions[0]
        for task_id in task_ids
        if (task_sessions := sessions_by_task.get(task_id))
    ]
    retry_tasks = {
        task_id: len(task_sessions)
        for task_id, task_sessions in sessions_by_task.items()
        if len(task_sessions) > 1
    }
    current_results = _current_result_summary(task_ids, result_payloads)
    trace_history = {
        "sessions": len(sessions),
        "accepted_sessions": sum(bool(item["accepted"]) for item in sessions),
        "failed_sessions": sum(not bool(item["accepted"]) for item in sessions),
        "tasks_with_trace": len(sessions_by_task),
        "tasks_with_retries": len(retry_tasks),
        "max_sessions_per_task": max(retry_tasks.values(), default=1 if sessions else 0),
        "first_session": _session_outcomes(first_sessions, len(task_ids)),
        "stop_reasons": dict(
            sorted(Counter(str(item["stop_reason"]) for item in sessions).items())
        ),
        "failed_stop_reasons": dict(
            sorted(
                Counter(
                    str(item["stop_reason"])
                    for item in sessions
                    if not item["accepted"]
                ).items()
            )
        ),
    }

    return {
        "schema_version": 1,
        "suite": "minif2f",
        "run_id": str(run_metadata.get("run_id", root.name)),
        "run_root": str(root),
        "split": run_metadata.get("split"),
        "execution_mode": execution_mode_from_proof_args(
            run_metadata.get("proof_args", ())
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "benchmark_revision": run_metadata.get("benchmark_revision"),
            "config_sha256": run_metadata.get("config_sha256"),
            "proof_args": run_metadata.get("proof_args", []),
        },
        "outcomes": {
            "summary_status": summary.get("status"),
            "selected": len(task_ids),
            "completed": summary.get("completed"),
            "accepted": current_results["accepted"],
            "proof_or_generation_failures": current_results[
                "proof_or_generation_failures"
            ],
            "infrastructure_failures": current_results[
                "infrastructure_failures"
            ],
            "pending": current_results["pending"],
            "current_results": current_results,
            "trace_history": trace_history,
        },
        "usage": {
            "current_accepted_sessions": _usage_rollup(
                current_accepted_sessions,
                expected_tasks=current_results["accepted"],
            ),
            "all_trace_sessions": _usage_rollup(
                sessions,
                expected_tasks=len(sessions_by_task),
            ),
            "ledger_coverage": _ledger_coverage(sessions),
            "provider_request_events": _provider_request_counts(sessions),
            "accepted_tasks_without_trace": accepted_without_trace,
            "semantics": {
                "current_accepted_sessions": (
                    "The latest trace session for every currently accepted task. "
                    "Earlier resume attempts are excluded."
                ),
                "all_trace_sessions": (
                    "All append-only trace sessions, including failed and resumed runs."
                ),
                "observed_value": (
                    "Sum of provider-reported usage only. Missing usage is excluded, "
                    "never converted to zero."
                ),
                "cached_tokens": (
                    "Cached input is a subset of input_tokens and must not be added "
                    "again to provider_total_tokens."
                ),
            },
        },
    }


def render_minif2f_usage_markdown(report: Mapping[str, Any]) -> str:
    """Render the stable high-level report fields for human inspection."""
    outcomes = report["outcomes"]
    history = outcomes["trace_history"]
    first = history["first_session"]
    current = report["usage"]["current_accepted_sessions"]
    all_sessions = report["usage"]["all_trace_sessions"]
    ledger = report["usage"]["ledger_coverage"]
    provider_requests = report["usage"]["provider_request_events"]
    lines = [
        f"# miniF2F usage report `{report['run_id']}`",
        "",
        f"- Split: `{report.get('split')}`",
        f"- Execution mode: `{report.get('execution_mode')}`",
        f"- Current accepted: {outcomes['accepted']} / {outcomes['selected']} "
        f"({_percent(outcomes['accepted'], outcomes['selected'])})",
        f"- First-session accepted: {first['accepted']} / "
        f"{first['tasks_with_session']} "
        f"({_percent(first['accepted'], first['tasks_with_session'])})",
        f"- Trace sessions: {history['sessions']} "
        f"({history['failed_sessions']} failed before recovery/current state)",
        f"- Tasks with resume history: {history['tasks_with_retries']}",
        "",
        "## Token usage",
        "",
        "| Scope | Sessions | Input | Cached input | Visible output | "
        "Reasoning | Provider total | Complete model-call coverage |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        _usage_markdown_row("Current accepted", current),
        _usage_markdown_row("All trace history", all_sessions),
        "",
        "`cached input` is already included in input tokens. Provider totals are "
        "observed response usage; missing provider usage is not treated as zero.",
        "",
        "## Per-task provider-total distribution",
        "",
        "| Scope | Mean | Median | P95 | Max |",
        "| --- | ---: | ---: | ---: | ---: |",
        _distribution_markdown_row("Current accepted", current),
        _distribution_markdown_row("All trace history", all_sessions),
        "",
        "## Measurement coverage",
        "",
        f"- Current accepted model calls with complete token usage: "
        f"{current['coverage']['complete_model_calls']} / "
        f"{current['coverage']['expected_model_calls']}",
        f"- All-history model calls with complete token usage: "
        f"{all_sessions['coverage']['complete_model_calls']} / "
        f"{all_sessions['coverage']['expected_model_calls']}",
        f"- Ledger billed-token status counts: "
        f"`{json.dumps(ledger['billed_tokens'], ensure_ascii=False, sort_keys=True)}`",
        f"- Ledger API-cost status counts: "
        f"`{json.dumps(ledger['api_cost_usd'], ensure_ascii=False, sort_keys=True)}`",
        f"- Provider request event status counts: "
        f"`{json.dumps(provider_requests, ensure_ascii=False, sort_keys=True)}`",
        "",
        "Model-call coverage describes aggregated usage records returned to the "
        "controller. Failed provider request events can still have unknown billed "
        "usage even when model-call coverage is complete.",
        "",
        "## Historical failed session reasons",
        "",
    ]
    failed_reasons = history["failed_stop_reasons"]
    if failed_reasons:
        lines.extend(
            f"- `{reason}`: {count}"
            for reason, count in failed_reasons.items()
        )
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def _trace_sessions(path: Path, task_id: str) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    unmatched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MiniF2FError(
                    f"{path}:{line_number}: invalid trace JSON: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise MiniF2FError(
                    f"{path}:{line_number}: trace event is not an object"
                )
            event_kind = event.get("event")
            run_id = str(event.get("run_id", ""))
            if event_kind == "run_summary":
                session = _session_from_summary(event, task_id)
                sessions.append(session)
                unmatched[run_id].append(session)
            elif event_kind == "cost_ledger_snapshot":
                candidates = unmatched.get(run_id, ())
                session = next(
                    (item for item in reversed(candidates) if item["ledger"] is None),
                    None,
                )
                if session is not None:
                    session["ledger"] = event.get("cost_ledger")
    return sessions


def _session_from_summary(event: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    budget = event.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw_usage = metadata.get("model_usage")
    usage_records = (
        [item for item in raw_usage if isinstance(item, dict)]
        if isinstance(raw_usage, list)
        else []
    )
    expected_model_calls = _nonnegative_int(budget.get("model_calls_used"))
    usage = {
        field: [
            value
            for item in usage_records
            if (value := _number(item.get(field))) is not None
        ]
        for field in TOKEN_FIELDS
    }
    complete_records = sum(
        all(_number(item.get(field)) is not None for field in TOKEN_FIELDS)
        for item in usage_records
    )
    return {
        "task_id": task_id,
        "run_id": str(event.get("run_id", "")),
        "accepted": bool(event.get("accepted")),
        "stop_reason": event.get("stop_reason"),
        "attempt_count": _nonnegative_int(event.get("attempt_count")),
        "checks_used": _nonnegative_int(budget.get("checks_used")),
        "expected_model_calls": expected_model_calls,
        "usage_records": len(usage_records),
        "complete_usage_records": complete_records,
        "usage": usage,
        "ledger": None,
    }


def _usage_rollup(
    sessions: Sequence[Mapping[str, Any]],
    *,
    expected_tasks: int,
) -> dict[str, Any]:
    expected_model_calls = sum(item["expected_model_calls"] for item in sessions)
    usage_records = sum(item["usage_records"] for item in sessions)
    complete_model_calls = sum(item["complete_usage_records"] for item in sessions)
    complete_sessions = sum(
        item["complete_usage_records"] == item["expected_model_calls"]
        for item in sessions
    )
    fields: dict[str, Any] = {}
    for field in TOKEN_FIELDS:
        values = [
            value
            for session in sessions
            for value in session["usage"][field]
        ]
        fields[field] = {
            "observed_value": _clean_number(sum(values)),
            "observed_model_calls": len(values),
            "expected_model_calls": expected_model_calls,
            "complete": len(values) == expected_model_calls,
        }

    per_task: dict[str, dict[str, float]] = defaultdict(
        lambda: {field: 0 for field in TOKEN_FIELDS}
    )
    incomplete_tasks: set[str] = set()
    for session in sessions:
        task_id = str(session["task_id"])
        if session["complete_usage_records"] != session["expected_model_calls"]:
            incomplete_tasks.add(task_id)
        for field in TOKEN_FIELDS:
            per_task[task_id][field] += sum(session["usage"][field])
    distributions = {
        field: _distribution(
            [values[field] for values in per_task.values()]
        )
        for field in TOKEN_FIELDS
    }
    return {
        "sessions": len(sessions),
        "tasks": len(per_task),
        "expected_tasks": expected_tasks,
        "coverage": {
            "sessions_with_complete_usage": complete_sessions,
            "sessions_total": len(sessions),
            "complete_model_calls": complete_model_calls,
            "usage_records": usage_records,
            "expected_model_calls": expected_model_calls,
            "tasks_with_incomplete_usage": sorted(incomplete_tasks),
        },
        "tokens": fields,
        "observed_per_task": distributions,
    }


def _current_result_summary(
    task_ids: Sequence[str],
    result_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    attempts: Counter[str] = Counter()
    checks: Counter[str] = Counter()
    model_calls: Counter[str] = Counter()
    accepted = proof_failures = infrastructure = pending = 0
    for task_id in task_ids:
        result = result_payloads.get(task_id)
        if not result:
            pending += 1
            continue
        if result.get("ok"):
            accepted += 1
        elif saved_result_is_infrastructure(dict(result)):
            infrastructure += 1
        else:
            proof_failures += 1
        _count_value(attempts, result.get("attempts"))
        _count_value(checks, result.get("checks_used"))
        _count_value(model_calls, result.get("model_calls_used"))
    return {
        "accepted": accepted,
        "proof_or_generation_failures": proof_failures,
        "infrastructure_failures": infrastructure,
        "pending": pending,
        "attempts_distribution": _counter_dict(attempts),
        "checks_distribution": _counter_dict(checks),
        "model_calls_distribution": _counter_dict(model_calls),
    }


def _session_outcomes(
    sessions: Sequence[Mapping[str, Any]],
    selected: int,
) -> dict[str, Any]:
    accepted = sum(bool(item["accepted"]) for item in sessions)
    infrastructure = sum(
        str(item["stop_reason"]).startswith("generation:provider_")
        or item["stop_reason"] in {"tool_unavailable"}
        for item in sessions
    )
    denominator = len(sessions) - infrastructure
    return {
        "selected": selected,
        "tasks_with_session": len(sessions),
        "accepted": accepted,
        "raw_accepted_rate": _rate(accepted, len(sessions)),
        "infrastructure_failures": infrastructure,
        "accepted_rate_excluding_infrastructure": _rate(accepted, denominator),
        "stop_reasons": dict(
            sorted(Counter(str(item["stop_reason"]) for item in sessions).items())
        ),
    }


def _ledger_coverage(sessions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    coverage = {field: Counter() for field in LEDGER_FIELDS}
    for session in sessions:
        ledger = session.get("ledger")
        totals = (
            ledger.get("reconciliation", {}).get("totals", {})
            if isinstance(ledger, dict)
            else {}
        )
        for field in LEDGER_FIELDS:
            measurement = totals.get(field)
            status = (
                measurement.get("measurement_status", "missing")
                if isinstance(measurement, dict)
                else "missing"
            )
            coverage[field][str(status)] += 1
    return {
        field: dict(sorted(statuses.items()))
        for field, statuses in coverage.items()
    }


def _provider_request_counts(
    sessions: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    statuses: Counter[str] = Counter()
    for session in sessions:
        ledger = session.get("ledger")
        events = ledger.get("events", ()) if isinstance(ledger, dict) else ()
        for event in events:
            if isinstance(event, dict) and event.get("kind") == "provider_request":
                statuses[str(event.get("status", "unknown"))] += 1
    return dict(sorted(statuses.items()))


def _distribution(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {
            "count": 0,
            "sum": None,
            "mean": None,
            "median": None,
            "p95": None,
            "max": None,
        }
    total = sum(ordered)
    return {
        "count": len(ordered),
        "sum": _clean_number(total),
        "mean": round(total / len(ordered), 2),
        "median": _clean_number(ordered[(len(ordered) - 1) // 2]),
        "p95": _clean_number(ordered[int((len(ordered) - 1) * 0.95)]),
        "max": _clean_number(ordered[-1]),
    }


def _usage_markdown_row(label: str, rollup: Mapping[str, Any]) -> str:
    tokens = rollup["tokens"]
    coverage = rollup["coverage"]
    return (
        f"| {label} | {rollup['sessions']:,} | "
        f"{tokens['input_tokens']['observed_value']:,} | "
        f"{tokens['cached_tokens']['observed_value']:,} | "
        f"{tokens['output_tokens']['observed_value']:,} | "
        f"{tokens['reasoning_tokens']['observed_value']:,} | "
        f"{tokens['provider_total_tokens']['observed_value']:,} | "
        f"{coverage['complete_model_calls']:,} / "
        f"{coverage['expected_model_calls']:,} |"
    )


def _distribution_markdown_row(label: str, rollup: Mapping[str, Any]) -> str:
    values = rollup["observed_per_task"]["provider_total_tokens"]
    return (
        f"| {label} | {values['mean']:,.2f} | {values['median']:,} | "
        f"{values['p95']:,} | {values['max']:,} |"
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MiniF2FError(f"missing benchmark run artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MiniF2FError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MiniF2FError(f"{path}: expected a JSON object")
    return payload


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _nonnegative_int(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _clean_number(value: float | int) -> float | int:
    return int(value) if float(value).is_integer() else value


def _count_value(counter: Counter[str], value: Any) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        counter[str(value)] += 1
    else:
        counter["unavailable"] += 1


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(
        sorted(
            counter.items(),
            key=lambda item: (
                item[0] == "unavailable",
                int(item[0]) if item[0].isdigit() else item[0],
            ),
        )
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _percent(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.2f}%" if denominator else "NA"
