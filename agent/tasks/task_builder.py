"""Build proof-completion tasks from Lean source files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .types import ProofTask, TaskInputKind


DEFAULT_HOLE_MARKER = "{{proof}}"


class TaskBuildError(ValueError):
    """Raised when a Lean source file cannot be converted into tasks."""


@dataclass(frozen=True)
class TaskBuilderConfig:
    """Configuration for Lean proof task extraction."""

    hole_marker: str = DEFAULT_HOLE_MARKER
    inactive_hole_fill: str = "sorry"
    default_split: str = "dev"
    allowed_retrieval_scope: tuple[str, ...] = ("same_file",)
    metadata_defaults: dict[str, Any] = field(default_factory=dict)
    allow_multiple_marker_tasks: bool = False
    allow_multiple_sorry_tasks: bool = False


@dataclass(frozen=True)
class HoleOccurrence:
    """Location of one editable proof hole in a Lean source file."""

    start: int
    end: int
    line: int
    column: int
    kind: str


class LeanTaskBuilder:
    """Create Lean proof-completion tasks with one active editable hole.

    This builder intentionally avoids a full Lean parser. It supports an
    explicit marker for curated tasks and a small tokenizer for standalone
    `sorry` holes that ignores comments and strings. Source files may contain
    multiple candidate holes when the corresponding config flag is enabled;
    each emitted task still has exactly one active marker for the controller to
    edit.
    """

    def __init__(self, config: TaskBuilderConfig | None = None) -> None:
        self.config = config or TaskBuilderConfig()

    def build_from_file(
        self,
        path: str | Path,
        *,
        split: str | None = None,
        task_id_prefix: str | None = None,
    ) -> list[ProofTask]:
        source_path = Path(path).resolve()
        source = source_path.read_text(encoding="utf-8")
        return self.build_from_source(
            source,
            source_path=source_path,
            split=split,
            task_id_prefix=task_id_prefix,
        )

    def build_from_directory(
        self,
        root: str | Path,
        *,
        split: str | None = None,
        pattern: str = "*.lean",
    ) -> list[ProofTask]:
        root_path = Path(root).resolve()
        tasks: list[ProofTask] = []
        for path in sorted(root_path.rglob(pattern)):
            tasks.extend(
                self.build_from_file(
                    path,
                    split=split,
                    task_id_prefix=_path_stem_id(path.relative_to(root_path)),
                )
            )
        return tasks

    def build_from_source(
        self,
        source: str,
        *,
        source_path: str | Path | None = None,
        split: str | None = None,
        task_id_prefix: str | None = None,
    ) -> list[ProofTask]:
        marker_occurrences = _find_literal_occurrences(source, self.config.hole_marker)
        if marker_occurrences:
            if len(marker_occurrences) > 1 and not self.config.allow_multiple_marker_tasks:
                raise TaskBuildError(
                    "Multiple explicit proof markers found. The MVP task builder "
                    "expects one editable hole per task source, or set "
                    "allow_multiple_marker_tasks=True to emit one task per marker."
                )
            return self._tasks_from_occurrences(
                source,
                occurrences=[
                    HoleOccurrence(
                        start=start,
                        end=end,
                        line=_line_col(source, start)[0],
                        column=_line_col(source, start)[1],
                        kind="marker",
                    )
                    for start, end in marker_occurrences
                ],
                source_path=source_path,
                split=split,
                task_id_prefix=task_id_prefix,
                original_hole_text=self.config.hole_marker,
            )

        sorry_occurrences = _find_standalone_token_occurrences(source, "sorry")
        if not sorry_occurrences:
            raise TaskBuildError("No explicit proof marker or standalone 'sorry' hole found.")
        if len(sorry_occurrences) > 1 and not self.config.allow_multiple_sorry_tasks:
            raise TaskBuildError(
                "Multiple standalone 'sorry' holes found. Split the source into one-hole "
                "tasks or set allow_multiple_sorry_tasks=True for extraction-only use."
            )

        return self._tasks_from_occurrences(
            source,
            occurrences=[
                HoleOccurrence(
                    start=start,
                    end=end,
                    line=line,
                    column=column,
                    kind="sorry",
                )
                for start, end, line, column in sorry_occurrences
            ],
            source_path=source_path,
            split=split,
            task_id_prefix=task_id_prefix,
            original_hole_text="sorry",
        )

    def to_jsonl(self, tasks: Iterable[ProofTask]) -> str:
        return "\n".join(json.dumps(_task_to_dict(task), ensure_ascii=False) for task in tasks)

    def _tasks_from_occurrences(
        self,
        source: str,
        *,
        occurrences: list[HoleOccurrence],
        source_path: str | Path | None,
        split: str | None,
        task_id_prefix: str | None,
        original_hole_text: str,
    ) -> list[ProofTask]:
        if not occurrences:
            return []

        path = Path(source_path).resolve() if source_path is not None else None
        base_id = task_id_prefix or (path.stem if path else "lean_task")
        imports = _extract_imports(source)
        split_name = split or self.config.default_split

        tasks: list[ProofTask] = []
        for index, occurrence in enumerate(occurrences):
            template = _source_with_active_hole(
                source,
                occurrences=occurrences,
                active_index=index,
                active_marker=self.config.hole_marker,
                inactive_fill=self.config.inactive_hole_fill,
            )
            metadata = {
                **self.config.metadata_defaults,
                "proof_system": "lean4",
                "source_file": str(path) if path else None,
                "split": split_name,
                "hole_kind": occurrence.kind,
                "hole_id": f"{_safe_task_id(base_id)}:{occurrence.line}:{occurrence.column}:{index}",
                "hole_index": index,
                "hole_line": occurrence.line,
                "hole_column": occurrence.column,
                "hole_start": occurrence.start,
                "hole_end": occurrence.end,
                "original_hole_text": original_hole_text,
                "active_hole_count": 1,
                "source_hole_count": len(occurrences),
                "inactive_hole_fill": self.config.inactive_hole_fill,
                "has_inactive_holes": len(occurrences) > 1,
                "source_imports": imports,
                "ground_truth_hidden": True,
                "allowed_retrieval_scope": self.config.allowed_retrieval_scope,
                "multiple_marker_extraction": self.config.allow_multiple_marker_tasks,
                "multiple_sorry_extraction": self.config.allow_multiple_sorry_tasks,
            }
            task_id = f"{_safe_task_id(base_id)}:{occurrence.line}:{occurrence.column}:{index}"
            tasks.append(
                ProofTask(
                    task_id=task_id,
                    source_template=template,
                    hole_marker=self.config.hole_marker,
                    imports=(),
                    input_kind=_task_input_kind(metadata),
                    metadata=metadata,
                )
            )
        return tasks


def _task_to_dict(task: ProofTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "source_template": task.source_template,
        "hole_marker": task.hole_marker,
        "imports": list(task.imports),
        "metadata": task.metadata,
    }


def _find_literal_occurrences(source: str, literal: str) -> list[tuple[int, int]]:
    if not literal:
        raise TaskBuildError("Hole marker must not be empty.")
    occurrences: list[tuple[int, int]] = []
    start = 0
    while True:
        index = source.find(literal, start)
        if index == -1:
            return occurrences
        occurrences.append((index, index + len(literal)))
        start = index + len(literal)


def _find_standalone_token_occurrences(
    source: str,
    token: str,
) -> list[tuple[int, int, int, int]]:
    occurrences: list[tuple[int, int, int, int]] = []
    i = 0
    line = 1
    column = 1
    block_depth = 0
    in_string = False

    def advance(text: str) -> None:
        nonlocal line, column
        for ch in text:
            if ch == "\n":
                line += 1
                column = 1
            else:
                column += 1

    while i < len(source):
        if block_depth == 0 and not in_string and source.startswith("--", i):
            next_newline = source.find("\n", i)
            if next_newline == -1:
                break
            advance(source[i : next_newline + 1])
            i = next_newline + 1
            continue

        if not in_string and source.startswith("/-", i):
            block_depth += 1
            advance(source[i : i + 2])
            i += 2
            continue

        if block_depth > 0:
            if source.startswith("-/", i):
                block_depth -= 1
                advance(source[i : i + 2])
                i += 2
            else:
                advance(source[i])
                i += 1
            continue

        ch = source[i]
        if ch == '"' and (i == 0 or source[i - 1] != "\\"):
            in_string = not in_string
            advance(ch)
            i += 1
            continue

        if in_string:
            advance(ch)
            i += 1
            continue

        if source.startswith(token, i) and _is_token_boundary(source, i, i + len(token)):
            occurrences.append((i, i + len(token), line, column))
            advance(source[i : i + len(token)])
            i += len(token)
            continue

        advance(ch)
        i += 1

    return occurrences


def _source_with_active_hole(
    source: str,
    *,
    occurrences: list[HoleOccurrence],
    active_index: int,
    active_marker: str,
    inactive_fill: str,
) -> str:
    parts: list[str] = []
    cursor = 0
    for index, occurrence in enumerate(occurrences):
        parts.append(source[cursor : occurrence.start])
        parts.append(active_marker if index == active_index else inactive_fill)
        cursor = occurrence.end
    parts.append(source[cursor:])
    return "".join(parts)


def _extract_imports(source: str) -> tuple[str, ...]:
    imports: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped.removeprefix("import ").strip())
    return tuple(imports)


def _line_col(source: str, offset: int) -> tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    last_newline = source.rfind("\n", 0, offset)
    column = offset + 1 if last_newline == -1 else offset - last_newline
    return line, column


def _is_token_boundary(source: str, start: int, end: int) -> bool:
    before = source[start - 1] if start > 0 else ""
    after = source[end] if end < len(source) else ""
    return not _is_identifier_char(before) and not _is_identifier_char(after)


def _is_identifier_char(ch: str) -> bool:
    return ch == "_" or ch.isalnum() or ord(ch) > 127


def _path_stem_id(path: Path) -> str:
    without_suffix = path.with_suffix("")
    return "_".join(without_suffix.parts)


def _safe_task_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", ":"} else "_" for ch in value)
    return cleaned or "lean_task"


def _task_input_kind(metadata: dict[str, Any]) -> TaskInputKind:
    value = metadata.get("input_kind")
    if isinstance(value, TaskInputKind):
        return value
    if isinstance(value, str):
        try:
            return TaskInputKind(value)
        except ValueError:
            return TaskInputKind.LEAN
    return TaskInputKind.LEAN
