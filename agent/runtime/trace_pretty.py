"""Human-oriented rendering for JSONL controller traces."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class TraceReadError(ValueError):
    """Raised when a trace contains an invalid JSONL record."""


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    """Read one JSON object per non-empty line with actionable diagnostics."""
    source = Path(path)
    events: list[dict[str, Any]] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TraceReadError(f"{source}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceReadError(
                f"{source}:{line_number}:{exc.colno}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise TraceReadError(f"{source}:{line_number}: event must be a JSON object")
        events.append(event)
    return events


def format_trace(
    events: Sequence[Mapping[str, Any]],
    *,
    source: str | Path | None = None,
    latest: bool = False,
    show_proof: bool = False,
    show_cost: bool = False,
    raw_events: bool = False,
    max_message_chars: int = 2_000,
) -> str:
    """Render the failure-first view used by ``scripts/trace_pretty.py``."""
    runs = _group_runs(events)
    if latest and runs:
        runs = runs[-1:]
    lines = [f"TRACE {source}" if source is not None else "TRACE"]
    if not runs:
        lines.append("  (no run_summary events)")
    for index, run in enumerate(runs, start=1):
        if index > 1:
            lines.append("")
        lines.extend(
            _format_run(
                run,
                index=index,
                total=len(runs),
                show_proof=show_proof,
                show_cost=show_cost,
                max_message_chars=max_message_chars,
            )
        )
    if raw_events:
        lines.extend(("", "RAW EVENTS", json.dumps(list(events), ensure_ascii=False, indent=2)))
    return "\n".join(lines)


def _group_runs(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == "run_summary":
            run = {"summary": event, "events": []}
            runs.append(run)
            by_id[str(event.get("run_id", f"run:{len(runs)}"))] = run
            continue
        run = by_id.get(str(event.get("run_id", "")))
        if run is not None:
            run["events"].append(event)
    return runs


def _format_run(
    run: Mapping[str, Any],
    *,
    index: int,
    total: int,
    show_proof: bool,
    show_cost: bool,
    max_message_chars: int,
) -> list[str]:
    summary = _mapping(run.get("summary"))
    events = [event for event in run.get("events", ()) if isinstance(event, Mapping)]
    task = _mapping(summary.get("task"))
    budget = _mapping(summary.get("budget"))
    metadata = _mapping(summary.get("metadata"))
    accepted = bool(summary.get("accepted"))
    lines = [
        (
            f"RUN {index}/{total} [{'ACCEPTED' if accepted else 'FAILED'}] "
            f"task={task.get('task_id', '?')} stop={summary.get('stop_reason', '?')}"
        ),
        (
            "  budget: "
            f"elapsed={_seconds(budget.get('elapsed_seconds'))} "
            f"model_calls={budget.get('model_calls_used', '?')} "
            f"checks={budget.get('checks_used', '?')} "
            f"attempts={summary.get('attempt_count', '?')}"
        ),
    ]

    failures = metadata.get("generation_failures")
    if isinstance(failures, (list, tuple)) and failures:
        lines.append("  GENERATION ERROR")
        for failure in failures:
            if not isinstance(failure, Mapping):
                continue
            reason = failure.get("reason", "unknown")
            model = failure.get("model")
            suffix = f" model={model}" if model else ""
            lines.append(f"    {reason}{suffix}")
            _append_block(lines, failure.get("message"), indent="      ", limit=max_message_chars)
            _append_provider_timeline(lines, failure.get("provider_requests"))
            _append_tool_timeline(lines, failure.get("tool_calls"))

    attempt_events = [event for event in events if event.get("event") == "attempt"]
    if attempt_events:
        lines.append("  LEAN ATTEMPTS")
        for event in attempt_events:
            attempt = _mapping(event.get("attempt"))
            check = _mapping(attempt.get("check_result"))
            feedback = _mapping(check.get("parsed_feedback"))
            state = "OK" if check.get("accepted") else "FAIL"
            location = ""
            if feedback.get("line") is not None:
                location = f" at {feedback.get('line')}:{feedback.get('column', '?')}"
            lines.append(
                f"    [{attempt.get('attempt_index', '?')}] {state} "
                f"{check.get('category', 'unknown')}{location} "
                f"elapsed={_seconds(check.get('elapsed_seconds'))}"
            )
            candidate = attempt.get("candidate_file")
            if candidate:
                lines.append(f"      candidate: {candidate}")
            message = feedback.get("message") or check.get("raw_output")
            _append_block(lines, message, indent="      ", limit=max_message_chars)
            goals = feedback.get("unsolved_goals")
            if isinstance(goals, (list, tuple)) and goals:
                lines.append("      goals:")
                for goal in goals:
                    _append_block(lines, goal, indent="        ", limit=max_message_chars)
            edit = _mapping(attempt.get("edit"))
            _append_tool_timeline(lines, _mapping(edit.get("metadata")).get("tool_calls"))
            if show_proof and edit.get("text"):
                lines.append("      proof:")
                _append_block(lines, edit.get("text"), indent="        ", limit=None)
    elif not accepted:
        lines.append("  LEAN ATTEMPTS: none (failure happened before candidate checking)")

    if show_cost:
        for event in events:
            if event.get("event") == "cost_ledger_snapshot":
                lines.extend(_format_cost(_mapping(event.get("cost_ledger"))))
    return lines


def _append_provider_timeline(lines: list[str], value: Any) -> None:
    if not isinstance(value, (list, tuple)) or not value:
        return
    lines.append("      provider timeline:")
    for request in value:
        if not isinstance(request, Mapping):
            continue
        parts = [
            f"status={request.get('status', '?')}",
            f"retry={request.get('retry_index', '?')}",
            f"time={_milliseconds(request.get('wall_time_ms'))}",
        ]
        if request.get("http_status") is not None:
            parts.append(f"http={request.get('http_status')}")
        if request.get("error"):
            parts.append(f"error={request.get('error')}")
        if request.get("request_id"):
            parts.append(f"id={request.get('request_id')}")
        usage = _mapping(request.get("token_usage"))
        if usage:
            parts.append(
                "tokens="
                f"{usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')}"
            )
        lines.append("        " + " ".join(parts))


def _append_tool_timeline(lines: list[str], value: Any) -> None:
    if not isinstance(value, (list, tuple)) or not value:
        return
    lines.append("      tool timeline:")
    for call in value:
        if not isinstance(call, Mapping):
            continue
        parts = [
            str(call.get("tool_kind", "unknown")),
            f"status={call.get('status', '?')}",
            f"time={_milliseconds(call.get('wall_time_ms'))}",
        ]
        if call.get("error"):
            parts.append(f"error={call.get('error')}")
        lines.append("        " + " ".join(parts))


def _format_cost(ledger: Mapping[str, Any]) -> list[str]:
    totals = _mapping(_mapping(ledger.get("reconciliation")).get("totals"))
    lines = ["  COST TOTALS"]
    if not totals:
        return [*lines, "    unavailable"]
    for key in ("api_cost_usd", "input_tokens", "output_tokens", "reasoning_tokens"):
        measurement = _mapping(totals.get(key))
        value = measurement.get("value")
        status = measurement.get("measurement_status", "unavailable")
        rendered = str(value) if value is not None else f"NA ({status})"
        lines.append(f"    {key}: {rendered}")
    return lines


def _append_block(
    lines: list[str],
    value: Any,
    *,
    indent: str,
    limit: int | None,
) -> None:
    if value is None or value == "":
        return
    text = str(value).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + " ...[truncated]"
    for original in text.splitlines() or [text]:
        wrapped = textwrap.wrap(original, width=max(20, 110 - len(indent))) or [""]
        lines.extend(indent + line for line in wrapped)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _seconds(value: Any) -> str:
    try:
        return f"{float(value):.3f}s"
    except (TypeError, ValueError):
        return "NA"


def _milliseconds(value: Any) -> str:
    try:
        return f"{float(value) / 1000:.3f}s"
    except (TypeError, ValueError):
        return "NA"
