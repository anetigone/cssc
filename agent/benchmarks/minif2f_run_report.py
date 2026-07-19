"""Outcome classification, summaries, and human-readable miniF2F run indexes."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.proof_system.base import DiagnosticCategory

from .minif2f import MiniF2FError


INFRASTRUCTURE_CATEGORIES = {
    DiagnosticCategory.CHECKER_ERROR,
    DiagnosticCategory.TIMEOUT,
    DiagnosticCategory.TOOL_UNAVAILABLE,
}
INFRASTRUCTURE_STOP_REASONS = {
    "generation:provider_error",
    "tool_unavailable",
}


def classify_infrastructure_failure(result: Any) -> tuple[bool, str | None]:
    """Classify failures that are external to mathematical/proof correctness."""
    stop_reason = str(result.stop_reason)
    if stop_reason in INFRASTRUCTURE_STOP_REASONS:
        return True, stop_reason
    if stop_reason.startswith("generation:"):
        # The terminal generation outcome is authoritative. Prior checker
        # attempts may contain infrastructure diagnostics, but they did not
        # cause this run to stop.
        is_provider_error = stop_reason.startswith("generation:provider_")
        return is_provider_error, stop_reason if is_provider_error else None
    if result.attempts:
        category = result.attempts[-1].check_result.category
        if category in INFRASTRUCTURE_CATEGORIES:
            return True, f"checker:{category.value}"
    return False, None


def saved_result_is_infrastructure(payload: dict[str, Any]) -> bool:
    """Recognize both current results and pre-fix provider-error results."""
    stop_reason = str(payload.get("stop_reason", ""))
    if stop_reason in INFRASTRUCTURE_STOP_REASONS:
        return True
    if stop_reason.startswith("generation:"):
        # Repair stale pre-fix flags on saved non-provider generation failures.
        return stop_reason.startswith("generation:provider_")
    return bool(payload.get("infrastructure_failure"))


def saved_result_is_transient_generation(payload: dict[str, Any]) -> bool:
    return payload.get("stop_reason") in {
        "generation:model_output_truncated",
        "generation:empty_model_output",
    }


def write_summary(
    root: Path,
    run_id: str,
    selected: int,
    completed: int,
    accepted: int,
    failed: int,
    skipped: int,
    infrastructure_failures: int,
    task_ids: Sequence[str],
    status: str,
    prior_error_history: Sequence[dict[str, Any]] = (),
    *,
    result_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    write_index: bool = True,
) -> None:
    failed_tasks, infrastructure_failure_tasks = failure_task_details(
        root, task_ids, result_payloads=result_payloads
    )
    error_history = merge_error_history(
        prior_error_history,
        (
            *(
                {**detail, "classification": "proof_or_generation"}
                for detail in failed_tasks
            ),
            *(
                {**detail, "classification": "infrastructure"}
                for detail in infrastructure_failure_tasks
            ),
        ),
    )
    run_metadata = json.loads((root / "run.json").read_text(encoding="utf-8"))
    summary_payload = {
        "schema_version": 1,
        "suite": "minif2f",
        "run_id": run_id,
        "status": status,
        "execution_mode": execution_mode_from_proof_args(
            run_metadata.get("proof_args", ())
        ),
        "selected": selected,
        "completed": completed,
        "accepted": accepted,
        "failed": failed,
        "skipped": skipped,
        "infrastructure_failures": infrastructure_failures,
        "failed_tasks": failed_tasks,
        "infrastructure_failure_tasks": infrastructure_failure_tasks,
        "error_history": error_history,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(root / "summary.json", summary_payload)
    if write_index:
        write_run_index(
            root,
            task_ids,
            summary_payload,
            result_payloads=result_payloads,
        )


def refresh_minif2f_run_index(root: str | Path) -> None:
    """Regenerate the human-readable index for an existing benchmark run."""
    run_root = Path(root).resolve()
    run_metadata = json.loads(
        (run_root / "run.json").read_text(encoding="utf-8")
    )
    summary = json.loads(
        (run_root / "summary.json").read_text(encoding="utf-8")
    )
    task_ids = run_metadata.get("task_ids")
    if not isinstance(task_ids, list) or not all(
        isinstance(task_id, str) for task_id in task_ids
    ):
        raise MiniF2FError("run.json does not contain a valid task_ids list")
    write_run_index(run_root, task_ids, summary)


def write_run_index(
    root: Path,
    task_ids: Sequence[str],
    summary: dict[str, Any],
    *,
    result_payloads: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    rows = task_index_rows(
        root, task_ids, result_payloads=result_payloads
    )
    columns = (
        "index",
        "task_id",
        "status",
        "classification",
        "attempts",
        "checks_used",
        "model_calls_used",
        "stop_reason",
        "message",
        "result_path",
        "trace_path",
    )
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(root / "task-index.csv", csv_buffer.getvalue())
    atomic_text(root / "README.md", run_index_markdown(root, rows, summary))


def task_index_rows(
    root: Path,
    task_ids: Sequence[str],
    *,
    result_payloads: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, task_id in enumerate(task_ids, start=1):
        task_root = root / "tasks" / task_id
        result_path = task_root / "result.json"
        trace_path = task_root / "trace.jsonl"
        payload = _result_payload(
            result_path,
            task_id,
            result_payloads=result_payloads,
        )
        status, classification = task_status(payload)
        message = payload.get("last_message", "")
        generation_failures = payload.get("generation_failures")
        if isinstance(generation_failures, list) and generation_failures:
            last_failure = generation_failures[-1]
            if isinstance(last_failure, dict):
                message = last_failure.get("message", message)
        rows.append(
            {
                "index": index,
                "task_id": task_id,
                "status": status,
                "classification": classification,
                "attempts": payload.get("attempts", ""),
                "checks_used": payload.get("checks_used", ""),
                "model_calls_used": payload.get("model_calls_used", ""),
                "stop_reason": payload.get("stop_reason", ""),
                "message": str(message or "").replace("\r", " ").replace("\n", " "),
                "result_path": (
                    f"tasks/{task_id}/result.json"
                    if result_path.is_file()
                    else ""
                ),
                "trace_path": (
                    f"tasks/{task_id}/trace.jsonl"
                    if trace_path.is_file()
                    else ""
                ),
            }
        )
    return rows


def task_status(payload: dict[str, Any]) -> tuple[str, str]:
    if not payload:
        return "pending", "pending"
    if payload.get("ok"):
        return "accepted", "accepted"
    if saved_result_is_infrastructure(payload):
        return "infrastructure_failure", "infrastructure"
    stop_reason = str(payload.get("stop_reason", ""))
    if stop_reason.startswith("generation:"):
        return "generation_failure", "proof_or_generation"
    return "proof_failure", "proof_or_generation"


def run_index_markdown(
    root: Path,
    rows: Sequence[dict[str, Any]],
    summary: dict[str, Any],
) -> str:
    current_failures = [
        row
        for row in rows
        if row["status"] not in {"accepted", "pending"}
    ]
    pending = [row for row in rows if row["status"] == "pending"]
    lines = [
        f"# miniF2F run `{summary.get('run_id', root.name)}`",
        "",
        f"- Status: `{summary.get('status', 'unknown')}`",
        f"- Progress: {summary.get('completed', 0)} / "
        f"{summary.get('selected', len(rows))}",
        f"- Accepted: {summary.get('accepted', 0)}",
        f"- Proof/generation failures: {summary.get('failed', 0)}",
        f"- Infrastructure failures: "
        f"{summary.get('infrastructure_failures', 0)}",
        f"- Pending: {len(pending)}",
        "",
        "All tasks are listed in [task-index.csv](task-index.csv). "
        "The tables below show current state only; `summary.json#error_history` "
        "retains failures from earlier resume attempts.",
        "",
        "## Current failures",
        "",
    ]
    if current_failures:
        lines.extend(
            [
                "| Task | Status | Stop reason | Message |",
                "| --- | --- | --- | --- |",
                *(
                    "| [{task}](tasks/{task}/) | `{status}` | `{reason}` | "
                    "{message} |".format(
                        task=row["task_id"],
                        status=row["status"],
                        reason=row["stop_reason"] or "",
                        message=markdown_cell(row["message"]),
                    )
                    for row in current_failures
                ),
            ]
        )
    else:
        lines.append("None.")
    lines.extend(["", "## Pending tasks", ""])
    if pending:
        lines.extend(
            f"- [{row['task_id']}](tasks/{row['task_id']}/)"
            for row in pending
        )
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def markdown_cell(value: Any) -> str:
    text = str(value or "").replace("|", r"\|").strip()
    return text if len(text) <= 240 else text[:237] + "..."


def failure_task_details(
    root: Path,
    task_ids: Sequence[str],
    *,
    result_payloads: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failed: list[dict[str, Any]] = []
    infrastructure: list[dict[str, Any]] = []
    for task_id in task_ids:
        result_path = root / "tasks" / task_id / "result.json"
        payload = _result_payload(
            result_path,
            task_id,
            result_payloads=result_payloads,
        )
        if not payload:
            continue
        if payload.get("ok"):
            continue
        detail: dict[str, Any] = {
            "task_id": task_id,
            "stop_reason": payload.get("stop_reason"),
        }
        if payload.get("last_category"):
            detail["last_category"] = payload["last_category"]
        if payload.get("last_message"):
            detail["message"] = payload["last_message"]
        generation_failures = payload.get("generation_failures")
        if isinstance(generation_failures, list) and generation_failures:
            last_failure = generation_failures[-1]
            if isinstance(last_failure, dict) and last_failure.get("message"):
                detail["message"] = last_failure["message"]
        if saved_result_is_infrastructure(payload):
            detail["kind"] = payload.get("infrastructure_failure_kind")
            infrastructure.append(detail)
        else:
            failed.append(detail)
    return failed, infrastructure


def load_error_history(
    root: Path,
    task_ids: Sequence[str],
    *,
    result_payloads: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load durable prior errors before resume can overwrite task results."""
    entries: list[dict[str, Any]] = []
    summary_path = root / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        saved_history = summary.get("error_history")
        if isinstance(saved_history, list):
            entries.extend(
                item for item in saved_history if isinstance(item, dict)
            )
        for field, classification in (
            ("failed_tasks", "proof_or_generation"),
            ("infrastructure_failure_tasks", "infrastructure"),
        ):
            details = summary.get(field)
            if isinstance(details, list):
                entries.extend(
                    {**item, "classification": classification}
                    for item in details
                    if isinstance(item, dict)
                )

    failed_tasks, infrastructure_tasks = failure_task_details(
        root, task_ids, result_payloads=result_payloads
    )
    entries.extend(
        {**detail, "classification": "proof_or_generation"}
        for detail in failed_tasks
    )
    entries.extend(
        {**detail, "classification": "infrastructure"}
        for detail in infrastructure_tasks
    )
    return merge_error_history((), entries)


def _result_payload(
    result_path: Path,
    task_id: str,
    *,
    result_payloads: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    if result_payloads is not None:
        return result_payloads.get(task_id, {})
    if not result_path.is_file():
        return {}
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def merge_error_history(
    previous: Sequence[dict[str, Any]],
    current: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve unique errors in stable first-seen order across resumes."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in (*previous, *current):
        normalized = dict(entry)
        key = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(normalized)
    return merged


def execution_mode_from_proof_args(proof_args: Sequence[str]) -> str:
    mode = "minimal"
    for index, argument in enumerate(proof_args):
        if argument.startswith("--execution-mode="):
            mode = argument.partition("=")[2]
        elif argument == "--execution-mode" and index + 1 < len(proof_args):
            mode = proof_args[index + 1]
    return mode


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
