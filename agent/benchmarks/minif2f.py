"""Prepare an external Google DeepMind miniF2F checkout for evaluation.

The upstream Lean files aggregate an entire split in one file.  This adapter
extracts each canonical theorem into an independent, single-hole scaffold.
The external checkout and generated artifacts are intentionally not vendored
into this project.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..tasks.task_builder import LeanTaskBuilder, TaskBuildError
from .minif2f_source import (
    EXPECTED_SPLIT_COUNTS,
    HOLE_MARKER,
    SOURCE_FILES,
    SOURCE_URL,
    MiniF2FDeclaration,
    MiniF2FError,
    extract_source_declarations,
    extract_split as _extract_split,
    normalize_newlines as _normalize_newlines,
    resolve_source_root,
    sha256_bytes as _sha256_bytes,
    sha256_text as _sha256_text,
    validate_source_layout as _validate_source_layout,
)


@dataclass(frozen=True)
class MiniF2FPreparedSuite:
    """Paths and counts produced by :func:`prepare_minif2f`."""

    output_root: Path
    manifest_path: Path
    provenance_path: Path
    source_revision: str
    split_counts: dict[str, int]


def resolve_minif2f_root(
    source_root: str | Path | None = None,
    *,
    repository_root: str | Path = ".",
) -> Path:
    """Resolve an explicit checkout or the repository-local ignored default."""
    return resolve_source_root(source_root, repository_root=repository_root)


def extract_declarations(
    source_root: str | Path,
    *,
    expected_split_counts: dict[str, int] | None = None,
) -> tuple[MiniF2FDeclaration, ...]:
    """Extract canonical, non-variant theorems from both upstream split files."""
    return extract_source_declarations(
        source_root,
        expected_split_counts=expected_split_counts,
    )


def prepare_minif2f(
    source_root: str | Path,
    output_root: str | Path,
    *,
    expected_split_counts: dict[str, int] | None = None,
    source_revision: str | None = None,
    source_url: str | None = None,
    allow_dirty_source: bool = False,
) -> MiniF2FPreparedSuite:
    """Generate ignored fixtures, a manifest, and provenance without running Lean."""
    root = Path(source_root).resolve()
    output = Path(output_root).resolve()
    declarations = extract_declarations(
        root,
        expected_split_counts=expected_split_counts,
    )
    revision = source_revision or _git_output(root, "rev-parse", "HEAD")
    if not revision:
        raise MiniF2FError(
            "could not determine source revision; pass source_revision explicitly"
        )
    dirty = _git_output(root, "status", "--porcelain", "--untracked-files=no")
    if dirty and not allow_dirty_source:
        raise MiniF2FError(
            "external miniF2F has tracked modifications; restore it or pass allow_dirty_source=True"
        )
    resolved_url = source_url or _git_output(root, "remote", "get-url", "origin") or SOURCE_URL

    manifest_rows: list[dict[str, Any]] = []
    for item in declarations:
        relative_source = Path("fixtures") / item.split / f"{item.task_id}.lean"
        fixture_path = output / relative_source
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(item.scaffold, encoding="utf-8", newline="\n")
        manifest_rows.append(
            {
                "schema_version": 1,
                "suite": "minif2f",
                "suite_version": f"google-deepmind@{revision[:12]}",
                "task_id": item.task_id,
                "split": item.split,
                "source": relative_source.as_posix(),
                "upstream_source": item.source_file,
                "upstream_line": item.source_line,
                "statement_sha256": item.statement_sha256,
                "scaffold_sha256": item.scaffold_sha256,
                "proof_system": "lean4",
                "ground_truth_hidden": True,
                "eligibility": "not_checked",
                "benchmark_revision": revision,
                "license": "Apache-2.0",
            }
        )

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.jsonl"
    manifest_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in manifest_rows),
        encoding="utf-8",
        newline="\n",
    )
    provenance = _build_provenance(
        root,
        revision=revision,
        source_url=resolved_url,
        dirty=bool(dirty),
        split_counts=_split_counts(declarations),
    )
    provenance_path = output / "provenance.json"
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    validate_prepared_minif2f(
        output,
        expected_split_counts=expected_split_counts,
    )
    return MiniF2FPreparedSuite(
        output_root=output,
        manifest_path=manifest_path,
        provenance_path=provenance_path,
        source_revision=revision,
        split_counts=_split_counts(declarations),
    )


def validate_prepared_minif2f(
    output_root: str | Path,
    *,
    expected_split_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Validate generated files, hashes, split isolation, and the one-hole invariant."""
    output = Path(output_root).resolve()
    manifest_path = output / "manifest.jsonl"
    provenance_path = output / "provenance.json"
    if not manifest_path.is_file() or not provenance_path.is_file():
        raise MiniF2FError("prepared output requires manifest.jsonl and provenance.json")

    rows = _read_jsonl(manifest_path)
    expected = expected_split_counts or EXPECTED_SPLIT_COUNTS
    counts = {split: 0 for split in SOURCE_FILES}
    task_ids: set[str] = set()
    builder = LeanTaskBuilder()
    for row in rows:
        task_id = row.get("task_id")
        split = row.get("split")
        if not isinstance(task_id, str) or not task_id:
            raise MiniF2FError("manifest row has an invalid task_id")
        if task_id in task_ids:
            raise MiniF2FError(f"duplicate manifest task_id: {task_id}")
        task_ids.add(task_id)
        if split not in counts:
            raise MiniF2FError(f"{task_id}: invalid split {split!r}")
        counts[split] += 1
        relative = row.get("source")
        if not isinstance(relative, str):
            raise MiniF2FError(f"{task_id}: source must be a relative path")
        fixture = (output / relative).resolve()
        try:
            fixture.relative_to(output)
        except ValueError as exc:
            raise MiniF2FError(f"{task_id}: fixture escapes output root") from exc
        if not fixture.is_file():
            raise MiniF2FError(f"{task_id}: missing fixture {relative}")
        scaffold = _normalize_newlines(fixture.read_text(encoding="utf-8"))
        if _sha256_text(scaffold) != row.get("scaffold_sha256"):
            raise MiniF2FError(f"{task_id}: scaffold hash mismatch")
        try:
            tasks = builder.build_from_source(scaffold, source_path=fixture, split=split)
        except TaskBuildError as exc:
            raise MiniF2FError(f"{task_id}: invalid single-hole scaffold: {exc}") from exc
        if len(tasks) != 1 or tasks[0].task_id != task_id:
            raise MiniF2FError(f"{task_id}: scaffold did not round-trip to exactly one named task")

    for split, wanted in expected.items():
        if counts.get(split) != wanted:
            raise MiniF2FError(f"prepared {split} count {counts.get(split)} != expected {wanted}")
    return counts


def _build_provenance(
    root: Path,
    *,
    revision: str,
    source_url: str,
    dirty: bool,
    split_counts: dict[str, int],
) -> dict[str, Any]:
    lake_manifest = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    dependencies = {
        package["name"]: {
            "rev": package.get("rev"),
            "input_rev": package.get("inputRev"),
            "url": package.get("url"),
        }
        for package in lake_manifest.get("packages", [])
        if isinstance(package, dict) and isinstance(package.get("name"), str)
    }
    source_hashes = {
        f"MiniF2F/{filename}": _sha256_bytes((root / "MiniF2F" / filename).read_bytes())
        for filename in SOURCE_FILES.values()
    }
    return {
        "schema_version": 1,
        "suite": "minif2f",
        "source_url": source_url,
        "source_revision": revision,
        "source_dirty": dirty,
        "license": "Apache-2.0",
        "license_sha256": _sha256_bytes((root / "LICENSE").read_bytes()),
        "lean_toolchain": (root / "lean-toolchain").read_text(encoding="utf-8").strip(),
        "dependencies": dependencies,
        "split_counts": split_counts,
        "source_hashes": source_hashes,
        "ground_truth_policy": "upstream proof material is not copied into prompts or retrieval",
        "eligibility": "not_checked",
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MiniF2FError(f"{path.name}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise MiniF2FError(f"{path.name}:{line_number}: row must be an object")
        rows.append(row)
    if not rows:
        raise MiniF2FError(f"{path.name}: no rows")
    return rows


def _git_output(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _split_counts(declarations: Iterable[MiniF2FDeclaration]) -> dict[str, int]:
    counts = {split: 0 for split in SOURCE_FILES}
    for item in declarations:
        counts[item.split] += 1
    return counts
