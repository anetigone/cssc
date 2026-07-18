"""Same-process miniF2F benchmark execution with a persistent Lean server."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from agent.cli.app import _run_controller
from agent.cli.output import result_payload
from agent.cli.parser import build_parser as build_cli_parser
from agent.proof_system.base import BudgetSlice
from agent.proof_system.lean import LeanAdapter
from agent.runtime.trace_store import JsonlTraceStore
from agent.runtime.workspace import EphemeralCheckWorkspace
from agent.tasks.task_builder import LeanTaskBuilder

from .minif2f import MiniF2FError
from .minif2f_run_report import (
    INFRASTRUCTURE_CATEGORIES,
    INFRASTRUCTURE_STOP_REASONS,
    atomic_json as _atomic_json,
    atomic_text as _atomic_text,
    classify_infrastructure_failure as _classify_infrastructure_failure,
    execution_mode_from_proof_args as _execution_mode_from_proof_args,
    failure_task_details as _failure_task_details,
    load_error_history as _load_error_history,
    markdown_cell as _markdown_cell,
    merge_error_history as _merge_error_history,
    refresh_minif2f_run_index,
    run_index_markdown as _run_index_markdown,
    saved_result_is_infrastructure as _saved_result_is_infrastructure,
    saved_result_is_transient_generation as _saved_result_is_transient_generation,
    task_index_rows as _task_index_rows,
    task_status as _task_status,
    write_run_index as _write_run_index,
    write_summary as _write_summary,
)


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
