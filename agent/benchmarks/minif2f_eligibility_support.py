"""Candidate materialization and evidence helpers for miniF2F eligibility."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..proof_system.base import CheckResult, DiagnosticCategory
from .minif2f import HOLE_MARKER, MiniF2FError


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")


def materialize_candidates(
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
            raise MiniF2FError(
                f"{task_id}: expected one proof marker before eligibility"
            )
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


def cross_task_references(
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    task_ids = {str(row["task_id"]) for row in rows}
    references: list[tuple[str, str]] = []
    for row in rows:
        task_id = str(row["task_id"])
        tokens = set(_IDENTIFIER_RE.findall(str(row["candidate_source"])))
        for reference in sorted((tokens & task_ids) - {task_id}):
            references.append((task_id, reference))
    return references


def write_aggregate(group: list[dict[str, Any]], path: Path) -> None:
    if not group:
        raise MiniF2FError("cannot check an empty eligibility batch")
    first = str(group[0]["candidate_source"])
    theorem_at = first.find("theorem ")
    if theorem_at < 0:
        raise MiniF2FError(
            f"{group[0]['task_id']}: generated candidate has no theorem"
        )
    preamble = first[:theorem_at].rstrip()
    declarations: list[str] = []
    for item in group:
        source = str(item["candidate_source"])
        start = source.find("theorem ")
        if start < 0:
            raise MiniF2FError(
                f"{item['task_id']}: generated candidate has no theorem"
            )
        declarations.append(source[start:].strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        preamble + "\n\n" + "\n\n".join(declarations) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def eligibility_status(result: CheckResult) -> str:
    if result.accepted:
        return "eligible"
    if result.category in {
        DiagnosticCategory.TIMEOUT,
        DiagnosticCategory.TOOL_UNAVAILABLE,
        DiagnosticCategory.CHECKER_ERROR,
    }:
        return "infrastructure_failure"
    return "ineligible"


def update_manifest_eligibility(
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
        item["eligibility_evidence"] = (
            summary_path.relative_to(prepared_root).as_posix()
        )
        updated.append(item)
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in updated
        ),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
