"""Same-process miniF2F benchmark execution with a persistent Lean server."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from agent.cli.app import _run_controller
from agent.cli.output import result_payload
from agent.cli.parser import build_parser as build_cli_parser
from agent.proof_system.base import BudgetSlice, DiagnosticCategory
from agent.proof_system.lean import LeanAdapter
from agent.runtime.trace_store import JsonlTraceStore
from agent.runtime.workspace import EphemeralCheckWorkspace
from agent.tasks.task_builder import LeanTaskBuilder

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


@dataclass(frozen=True)
class MiniF2FRunSummary:
    run_id: str
    run_root: Path
    selected: int
    completed: int
    accepted: int
    failed: int
    skipped: int
    infrastructure_failures: int


def run_minif2f_benchmark(
    prepared_root: str | Path,
    project_root: str | Path,
    run_root: str | Path,
    *,
    split: str,
    proof_args: Sequence[str] = (),
    task_ids: Sequence[str] = (),
    offset: int = 0,
    limit: int | None = None,
    resume: bool = False,
    retry_infrastructure_failures: bool | None = None,
    retry_transient_generation_failures: bool | None = None,
    continue_on_infrastructure_failure: bool = False,
    progress: Callable[[int, int, str, str], None] | None = None,
) -> MiniF2FRunSummary:
    """Run independent tasks while sharing exactly one required Lean server."""
    prepared = Path(prepared_root).resolve()
    project = Path(project_root).resolve()
    root = Path(run_root).resolve()
    manifest = prepared / "manifest.jsonl"
    provenance_path = prepared / "provenance.json"
    if split not in {"valid", "test"}:
        raise MiniF2FError("split must be 'valid' or 'test'")
    if retry_infrastructure_failures is None:
        retry_infrastructure_failures = resume
    if retry_transient_generation_failures is None:
        retry_transient_generation_failures = resume
    if retry_infrastructure_failures and not resume:
        raise MiniF2FError("retry_infrastructure_failures requires resume=True")
    if retry_transient_generation_failures and not resume:
        raise MiniF2FError(
            "retry_transient_generation_failures requires resume=True"
        )
    if not manifest.is_file() or not provenance_path.is_file():
        raise MiniF2FError("prepared miniF2F manifest/provenance is missing")
    if not (project / "lakefile.lean").is_file() and not (project / "lakefile.toml").is_file():
        raise MiniF2FError(f"miniF2F project is not a Lake project: {project}")

    rows = [
        row for row in _read_jsonl(manifest)
        if row.get("split") == split and row.get("eligibility") == "eligible"
    ]
    if task_ids:
        wanted = set(task_ids)
        rows = [row for row in rows if row.get("task_id") in wanted]
        missing = wanted - {str(row["task_id"]) for row in rows}
        if missing:
            raise MiniF2FError(
                "requested task ids are absent or not eligible: " + ", ".join(sorted(missing))
            )
    rows = rows[offset : (offset + limit) if limit is not None else None]
    if not rows:
        raise MiniF2FError("task selection is empty")

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    benchmark_revision = str(rows[0].get("benchmark_revision", ""))
    config = {
        "schema_version": 1,
        "suite": "minif2f",
        "benchmark_revision": benchmark_revision,
        "split": split,
        "task_ids": [str(row["task_id"]) for row in rows],
        "proof_args": list(proof_args),
        "project_root": str(project),
        "persistent_lean_server_required": True,
    }
    config_hash = _sha256_json(config)
    run_id = root.name
    selected_task_ids = [str(row["task_id"]) for row in rows]
    run_metadata_path = root / "run.json"
    if resume:
        if not run_metadata_path.is_file():
            raise MiniF2FError(f"cannot resume: missing {run_metadata_path}")
        previous = json.loads(run_metadata_path.read_text(encoding="utf-8"))
        if previous.get("config_sha256") != config_hash:
            raise MiniF2FError("resume configuration differs from the existing run")
    else:
        if root.exists():
            raise MiniF2FError(f"run directory already exists: {root}")
        root.mkdir(parents=True)
        _atomic_json(
            run_metadata_path,
            {
                **config,
                "run_id": run_id,
                "config_sha256": config_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "prepared_provenance": provenance,
                "status": "running",
            },
        )

    error_history = (
        _load_error_history(root, selected_task_ids) if resume else []
    )
    completed = accepted = failed = skipped = infrastructure_failures = 0
    if resume:
        for row in rows:
            result_path = root / "tasks" / str(row["task_id"]) / "result.json"
            if not result_path.is_file():
                continue
            previous_result = json.loads(result_path.read_text(encoding="utf-8"))
            infrastructure = _saved_result_is_infrastructure(previous_result)
            if (
                (retry_infrastructure_failures and infrastructure)
                or (
                    retry_transient_generation_failures
                    and _saved_result_is_transient_generation(previous_result)
                )
            ):
                continue
            completed += 1
            skipped += 1
            accepted += int(bool(previous_result.get("ok")))
            failed += int(not bool(previous_result.get("ok")) and not infrastructure)
            infrastructure_failures += int(infrastructure)
    if completed == len(rows):
        _write_summary(
            root, run_id, len(rows), completed, accepted, failed, skipped,
            infrastructure_failures, selected_task_ids, "complete", error_history,
        )
        metadata = json.loads(run_metadata_path.read_text(encoding="utf-8"))
        metadata["status"] = "complete"
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(run_metadata_path, metadata)
        return MiniF2FRunSummary(
            run_id, root, len(rows), completed, accepted, failed, skipped,
            infrastructure_failures,
        )

    first_fixture = prepared / str(rows[0]["source"])
    cli_args = build_cli_parser().parse_args(
        ["prove", str(first_fixture), "--project-root", str(project), *proof_args]
    )
    cli_args.agent_root = str(Path.cwd().resolve())
    if cli_args.no_lean_server:
        raise MiniF2FError("formal benchmark runs forbid --no-lean-server")

    adapter = LeanAdapter(
        project_root=project,
        prefer_lake=not cli_args.no_lake,
        disallow_sorry=not cli_args.allow_sorry,
        lean_executable=cli_args.lean_executable,
        lake_executable=cli_args.lake_executable,
        use_server=True,
        require_server=True,
        server_startup_timeout_seconds=cli_args.lean_server_startup_timeout,
        server_fallback_seconds=cli_args.lean_server_fallback_seconds,
    )
    check_workspace = EphemeralCheckWorkspace(
        project / ".checks" / f"cssc-minif2f-{run_id}",
        keep_files=cli_args.keep_check_files,
    )
    aborted = False
    try:
        _prewarm(adapter, check_workspace.root, timeout_seconds=cli_args.lean_timeout)
        services = SimpleNamespace(adapter=adapter)
        builder = LeanTaskBuilder()
        for index, row in enumerate(rows, start=1):
            task_id = str(row["task_id"])
            task_root = root / "tasks" / task_id
            result_path = task_root / "result.json"
            if resume and result_path.is_file():
                previous_result = json.loads(result_path.read_text(encoding="utf-8"))
                if not (
                    (
                        retry_infrastructure_failures
                        and _saved_result_is_infrastructure(previous_result)
                    )
                    or (
                        retry_transient_generation_failures
                        and _saved_result_is_transient_generation(previous_result)
                    )
                ):
                    if progress:
                        progress(index, len(rows), task_id, "skipped")
                    continue

            fixture = prepared / str(row["source"])
            tasks = builder.build_from_file(fixture, split=split)
            if len(tasks) != 1 or tasks[0].task_id != task_id:
                raise MiniF2FError(f"{task_id}: fixture no longer round-trips to one task")
            task_root.mkdir(parents=True, exist_ok=True)
            result = _run_controller(
                cli_args,
                tasks[0],
                services,
                task_root / "candidates",
                check_workspace,
                project,
            )
            JsonlTraceStore(
                task_root / "trace.jsonl",
                include_raw_output=cli_args.trace_raw_output,
            ).append_result(result)
            payload = result_payload(result)
            payload.update(
                {
                    "schema_version": 1,
                    "suite": "minif2f",
                    "split": split,
                    "benchmark_revision": benchmark_revision,
                    "config_sha256": config_hash,
                }
            )
            infrastructure, infrastructure_kind = _classify_infrastructure_failure(result)
            payload["infrastructure_failure"] = infrastructure
            payload["infrastructure_failure_kind"] = infrastructure_kind
            _atomic_json(result_path, payload)
            completed += 1
            accepted += int(result.accepted)
            failed += int(not result.accepted and not infrastructure)
            infrastructure_failures += int(infrastructure)
            _write_summary(
                root, run_id, len(rows), completed, accepted, failed, skipped,
                infrastructure_failures,
                selected_task_ids,
                "running",
                error_history,
            )
            if progress:
                progress(index, len(rows), task_id, "accepted" if result.accepted else "failed")
            if infrastructure and not continue_on_infrastructure_failure:
                aborted = True
                break
    finally:
        adapter.close()

    status = "aborted_infrastructure" if aborted else "complete"
    _write_summary(
        root, run_id, len(rows), completed, accepted, failed, skipped,
        infrastructure_failures, selected_task_ids, status, error_history,
    )
    metadata = json.loads(run_metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = status
    metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(run_metadata_path, metadata)
    return MiniF2FRunSummary(
        run_id=run_id,
        run_root=root,
        selected=len(rows),
        completed=completed,
        accepted=accepted,
        failed=failed,
        skipped=skipped,
        infrastructure_failures=infrastructure_failures,
    )


def _classify_infrastructure_failure(result: Any) -> tuple[bool, str | None]:
    """Classify failures that are external to mathematical/proof correctness."""
    if result.stop_reason in INFRASTRUCTURE_STOP_REASONS:
        return True, result.stop_reason
    if result.stop_reason.startswith("generation:provider_"):
        return True, result.stop_reason
    if result.attempts:
        category = result.attempts[-1].check_result.category
        if category in INFRASTRUCTURE_CATEGORIES:
            return True, f"checker:{category.value}"
    return False, None


def _saved_result_is_infrastructure(payload: dict[str, Any]) -> bool:
    """Recognize both current results and pre-fix provider-error results."""
    if payload.get("infrastructure_failure"):
        return True
    stop_reason = str(payload.get("stop_reason", ""))
    return (
        stop_reason in INFRASTRUCTURE_STOP_REASONS
        or stop_reason.startswith("generation:provider_")
    )


def _saved_result_is_transient_generation(payload: dict[str, Any]) -> bool:
    return payload.get("stop_reason") == "generation:model_output_truncated"


def _prewarm(adapter: LeanAdapter, root: Path, *, timeout_seconds: float) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "Prewarm.lean"
    path.write_text("import Mathlib\n\nexample : True := by trivial\n", encoding="utf-8")
    try:
        result = adapter.check(path, BudgetSlice(timeout_seconds=timeout_seconds))
    finally:
        path.unlink(missing_ok=True)
    if not result.accepted:
        raise MiniF2FError(
            f"persistent Lean server prewarm failed ({result.category.value}): {result.raw_output}"
        )
    if "--server" not in result.command:
        raise MiniF2FError("prewarm did not use the persistent Lean server")


def _write_summary(
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
) -> None:
    failed_tasks, infrastructure_failure_tasks = _failure_task_details(
        root, task_ids
    )
    error_history = _merge_error_history(
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
        "execution_mode": _execution_mode_from_proof_args(
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
    _atomic_json(root / "summary.json", summary_payload)
    _write_run_index(root, task_ids, summary_payload)


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
    _write_run_index(run_root, task_ids, summary)


def _write_run_index(
    root: Path,
    task_ids: Sequence[str],
    summary: dict[str, Any],
) -> None:
    rows = _task_index_rows(root, task_ids)
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
    _atomic_text(root / "task-index.csv", csv_buffer.getvalue())
    _atomic_text(root / "README.md", _run_index_markdown(root, rows, summary))


def _task_index_rows(
    root: Path, task_ids: Sequence[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, task_id in enumerate(task_ids, start=1):
        task_root = root / "tasks" / task_id
        result_path = task_root / "result.json"
        trace_path = task_root / "trace.jsonl"
        payload: dict[str, Any] = {}
        if result_path.is_file():
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        status, classification = _task_status(payload)
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
                    f"tasks/{task_id}/result.json" if result_path.is_file() else ""
                ),
                "trace_path": (
                    f"tasks/{task_id}/trace.jsonl" if trace_path.is_file() else ""
                ),
            }
        )
    return rows


def _task_status(payload: dict[str, Any]) -> tuple[str, str]:
    if not payload:
        return "pending", "pending"
    if payload.get("ok"):
        return "accepted", "accepted"
    if _saved_result_is_infrastructure(payload):
        return "infrastructure_failure", "infrastructure"
    stop_reason = str(payload.get("stop_reason", ""))
    if stop_reason.startswith("generation:"):
        return "generation_failure", "proof_or_generation"
    return "proof_failure", "proof_or_generation"


def _run_index_markdown(
    root: Path,
    rows: Sequence[dict[str, Any]],
    summary: dict[str, Any],
) -> str:
    current_failures = [
        row for row in rows
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
                        message=_markdown_cell(row["message"]),
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


def _markdown_cell(value: Any) -> str:
    text = str(value or "").replace("|", r"\|").strip()
    return text if len(text) <= 240 else text[:237] + "..."


def _failure_task_details(
    root: Path, task_ids: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failed: list[dict[str, Any]] = []
    infrastructure: list[dict[str, Any]] = []
    for task_id in task_ids:
        result_path = root / "tasks" / task_id / "result.json"
        if not result_path.is_file():
            continue
        payload = json.loads(result_path.read_text(encoding="utf-8"))
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
        if _saved_result_is_infrastructure(payload):
            detail["kind"] = payload.get("infrastructure_failure_kind")
            infrastructure.append(detail)
        else:
            failed.append(detail)
    return failed, infrastructure


def _load_error_history(
    root: Path, task_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Load durable prior errors before resume can overwrite task results."""
    entries: list[dict[str, Any]] = []
    summary_path = root / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        saved_history = summary.get("error_history")
        if isinstance(saved_history, list):
            entries.extend(item for item in saved_history if isinstance(item, dict))
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

    failed_tasks, infrastructure_tasks = _failure_task_details(root, task_ids)
    entries.extend(
        {**detail, "classification": "proof_or_generation"}
        for detail in failed_tasks
    )
    entries.extend(
        {**detail, "classification": "infrastructure"}
        for detail in infrastructure_tasks
    )
    return _merge_error_history((), entries)


def _merge_error_history(
    previous: Sequence[dict[str, Any]],
    current: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve unique errors in stable first-seen order across resumes."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in (*previous, *current):
        normalized = dict(entry)
        key = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(normalized)
    return merged


def _execution_mode_from_proof_args(proof_args: Sequence[str]) -> str:
    mode = "minimal"
    for index, argument in enumerate(proof_args):
        if argument.startswith("--execution-mode="):
            mode = argument.partition("=")[2]
        elif argument == "--execution-mode" and index + 1 < len(proof_args):
            mode = proof_args[index + 1]
    return mode


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_text(path: Path, content: str) -> None:
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
