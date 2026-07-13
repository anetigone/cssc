"""Lean elaboration eligibility checks for prepared miniF2F tasks."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ..proof_system.base import BudgetSlice, CheckResult, DiagnosticCategory
from ..proof_system.lean import LeanAdapter
from .minif2f import HOLE_MARKER, MiniF2FError, validate_prepared_minif2f

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")


class EligibilityChecker(Protocol):
    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class MiniF2FEligibilitySummary:
    run_id: str
    results_path: Path
    summary_path: Path
    total: int
    eligible: int
    ineligible: int
    infrastructure_failure: int
    categories: dict[str, int]


def run_minif2f_eligibility(
    prepared_root: str | Path,
    project_root: str | Path,
    *,
    timeout_seconds: float = 900.0,
    lake_executable: str | None = None,
    checker: EligibilityChecker | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
    expected_split_counts: dict[str, int] | None = None,
    reuse_results: str | Path | None = None,
) -> MiniF2FEligibilitySummary:
    """Elaborate all tasks after a cross-task dependency audit.

    Repeatedly starting Lean for 488 files is prohibitively expensive on
    Windows.  We first prove that no scaffold mentions another benchmark task
    identifier, then check one aggregate file per split.  Ordinary theorem
    declarations do not add instances, notation, or attributes, so with zero
    cross-task references a successful aggregate elaboration establishes the
    same statement eligibility.  Failed batches are bisected down to single
    tasks so one bad declaration cannot taint an entire split.
    """
    prepared = Path(prepared_root).resolve()
    project = Path(project_root).resolve()
    validate_prepared_minif2f(prepared, expected_split_counts=expected_split_counts)
    manifest_path = prepared / "manifest.jsonl"
    provenance_path = prepared / "provenance.json"
    rows = _read_jsonl(manifest_path)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    revision = str(provenance.get("source_revision") or "unknown")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{revision[:12]}"
    run_root = prepared / "eligibility_runs" / run_id
    candidates_root = run_root / "candidates"
    batches_root = run_root / "batches"
    results_path = run_root / "results.jsonl"
    summary_path = run_root / "summary.json"
    run_root.mkdir(parents=True, exist_ok=False)

    prepared_rows = _materialize_candidates(prepared, rows, candidates_root)
    cross_references = _cross_task_references(prepared_rows)
    if cross_references:
        sample = ", ".join(
            f"{task}->{reference}" for task, reference in cross_references[:10]
        )
        raise MiniF2FError(
            "aggregate eligibility is unsafe because statements reference other "
            f"benchmark tasks: {sample}"
        )

    owned_checker = checker is None
    active_checker = checker or LeanAdapter(
        project_root=project,
        prefer_lake=True,
        disallow_sorry=False,
        lake_executable=lake_executable,
        use_server=False,
    )
    statuses: dict[str, dict[str, Any]] = {}
    category_counts: Counter[str] = Counter()
    completed = 0
    reused_count = 0

    if reuse_results is not None:
        reusable_path = Path(reuse_results).resolve()
        reusable = {str(row["task_id"]): row for row in _read_jsonl(reusable_path)}
        for item in prepared_rows:
            task_id = str(item["task_id"])
            previous = reusable.get(task_id)
            candidate_sha256 = _sha256_file(Path(item["candidate_path"]))
            if (
                previous is not None
                and previous.get("eligibility") == "eligible"
                and previous.get("candidate_sha256") == candidate_sha256
            ):
                evidence = dict(previous)
                evidence["run_id"] = run_id
                evidence["reused_from_run_id"] = previous.get("run_id")
                evidence["reused_from_results"] = str(reusable_path)
                statuses[task_id] = evidence
                category_counts[str(evidence["diagnostic_category"])] += 1
                completed += 1
                reused_count += 1
                if progress is not None:
                    progress(completed, len(rows), task_id, "eligible")

    def record(group: list[dict[str, Any]], result: CheckResult, batch: Path) -> None:
        nonlocal completed
        category = result.category.value
        status = _eligibility_status(result)
        for item in group:
            task_id = str(item["task_id"])
            evidence = {
                "schema_version": 1,
                "run_id": run_id,
                "task_id": task_id,
                "split": item["split"],
                "eligibility": status,
                "diagnostic_category": category,
                "exit_code": result.exit_code,
                "elapsed_seconds": result.elapsed_seconds,
                "command": list(result.command),
                "candidate_sha256": _sha256_file(Path(item["candidate_path"])),
                "batch_file": batch.relative_to(run_root).as_posix(),
                "batch_size": len(group),
                "batch_sha256": _sha256_file(batch),
                "diagnostic_output": result.raw_output,
            }
            statuses[task_id] = evidence
            category_counts[category] += 1
            completed += 1
            if progress is not None:
                progress(completed, len(rows), task_id, status)

    def check_group(group: list[dict[str, Any]], label: str) -> None:
        batch = batches_root / f"{label}.lean"
        _write_aggregate(group, batch)
        result = active_checker.check(batch, BudgetSlice(timeout_seconds=timeout_seconds))
        if result.accepted or len(group) == 1:
            record(group, result, batch)
            return
        if result.category == DiagnosticCategory.TOOL_UNAVAILABLE:
            record(group, result, batch)
            return
        middle = len(group) // 2
        check_group(group[:middle], f"{label}-a")
        check_group(group[middle:], f"{label}-b")

    try:
        for split in ("valid", "test"):
            split_rows = [
                row
                for row in prepared_rows
                if row["split"] == split and str(row["task_id"]) not in statuses
            ]
            if split_rows:
                check_group(split_rows, split)
    finally:
        if owned_checker:
            active_checker.close()

    with results_path.open("w", encoding="utf-8", newline="\n") as results_file:
        for row in rows:
            evidence = statuses[str(row["task_id"])]
            results_file.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n")

    status_counts = Counter(item["eligibility"] for item in statuses.values())
    summary_payload = {
        "schema_version": 1,
        "suite": "minif2f",
        "run_id": run_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_revision": revision,
        "lean_toolchain": provenance.get("lean_toolchain"),
        "mode": "split_aggregate_with_cross_task_reference_audit_and_bisection",
        "proof_fill": "sorry",
        "proof_acceptance_claimed": False,
        "independence_audit": {
            "task_identifier_count": len(rows),
            "cross_task_references": 0,
        },
        "reused_eligible_evidence": reused_count,
        "total": len(rows),
        "eligible": status_counts["eligible"],
        "ineligible": status_counts["ineligible"],
        "infrastructure_failure": status_counts["infrastructure_failure"],
        "diagnostic_categories": dict(sorted(category_counts.items())),
        "results_sha256": _sha256_file(results_path),
    }
    summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _update_manifest_eligibility(manifest_path, rows, statuses, run_id, summary_path, prepared)
    provenance["eligibility"] = {
        "status": "checked",
        "latest_run_id": run_id,
        "summary": summary_path.relative_to(prepared).as_posix(),
        "total": len(rows),
        "eligible": status_counts["eligible"],
        "ineligible": status_counts["ineligible"],
        "infrastructure_failure": status_counts["infrastructure_failure"],
    }
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return MiniF2FEligibilitySummary(
        run_id=run_id,
        results_path=results_path,
        summary_path=summary_path,
        total=len(rows),
        eligible=status_counts["eligible"],
        ineligible=status_counts["ineligible"],
        infrastructure_failure=status_counts["infrastructure_failure"],
        categories=dict(sorted(category_counts.items())),
    )


def _materialize_candidates(
    prepared: Path,
    rows: list[dict[str, Any]],
    candidates_root: Path,
) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["task_id"])
        split = str(row["split"])
        scaffold = (prepared / str(row["source"])).read_text(encoding="utf-8")
        if scaffold.count(HOLE_MARKER) != 1:
            raise MiniF2FError(f"{task_id}: expected one proof marker before eligibility")
        candidate = candidates_root / split / f"{task_id}.lean"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(
            scaffold.replace(HOLE_MARKER, "sorry"),
            encoding="utf-8",
            newline="\n",
        )
        item = dict(row)
        item["candidate_path"] = str(candidate)
        item["candidate_source"] = candidate.read_text(encoding="utf-8")
        materialized.append(item)
    return materialized


def _cross_task_references(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    task_ids = {str(row["task_id"]) for row in rows}
    references: list[tuple[str, str]] = []
    for row in rows:
        task_id = str(row["task_id"])
        tokens = set(_IDENTIFIER_RE.findall(str(row["candidate_source"])))
        for reference in sorted((tokens & task_ids) - {task_id}):
            references.append((task_id, reference))
    return references


def _write_aggregate(group: list[dict[str, Any]], path: Path) -> None:
    if not group:
        raise MiniF2FError("cannot check an empty eligibility batch")
    first = str(group[0]["candidate_source"])
    theorem_at = first.find("theorem ")
    if theorem_at < 0:
        raise MiniF2FError(f"{group[0]['task_id']}: generated candidate has no theorem")
    preamble = first[:theorem_at].rstrip()
    declarations: list[str] = []
    for item in group:
        source = str(item["candidate_source"])
        start = source.find("theorem ")
        if start < 0:
            raise MiniF2FError(f"{item['task_id']}: generated candidate has no theorem")
        declarations.append(source[start:].strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        preamble + "\n\n" + "\n\n".join(declarations) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _eligibility_status(result: CheckResult) -> str:
    if result.accepted:
        return "eligible"
    if result.category in {
        DiagnosticCategory.TIMEOUT,
        DiagnosticCategory.TOOL_UNAVAILABLE,
        DiagnosticCategory.CHECKER_ERROR,
    }:
        return "infrastructure_failure"
    return "ineligible"


def _update_manifest_eligibility(
    path: Path,
    rows: list[dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
    run_id: str,
    summary_path: Path,
    prepared_root: Path,
) -> None:
    updated: list[dict[str, Any]] = []
    for row in rows:
        evidence = statuses[str(row["task_id"])]
        item = dict(row)
        item["eligibility"] = evidence["eligibility"]
        item["eligibility_run_id"] = run_id
        item["eligibility_category"] = evidence["diagnostic_category"]
        item["eligibility_evidence"] = summary_path.relative_to(prepared_root).as_posix()
        updated.append(item)
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in updated),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
