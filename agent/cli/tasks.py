"""Task building and selection for the Lean task-solving CLI."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from agent import LeanTaskBuilder, ProofTask, TaskBuildError, TaskBuilderConfig

from .config import _config_has_inline_task_source
from .paths import resolve_agent_path


def build_tasks(args: Namespace) -> list[ProofTask]:
    config = TaskBuilderConfig(
        hole_marker=args.hole_marker,
        inactive_hole_fill=args.inactive_hole_fill,
        allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
        allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
    )
    builder = LeanTaskBuilder(config)
    task_config = getattr(args, "_task_config_data", None)
    if isinstance(task_config, dict) and _config_has_inline_task_source(task_config):
        tasks = _build_tasks_from_config(builder, args, task_config)
    else:
        source = resolve_agent_path(Path(args.agent_root), _require_source(args))
        if source.is_dir():
            tasks = builder.build_from_directory(source, split=args.split, pattern=args.pattern)
        else:
            tasks = builder.build_from_file(source, split=args.split)
    if not tasks:
        raise TaskBuildError("No tasks were extracted from task input.")
    return tasks


def _build_tasks_from_config(
    builder: LeanTaskBuilder,
    args: Namespace,
    config: dict[str, Any],
) -> list[ProofTask]:
    entries = config.get("tasks")
    if entries is None:
        entries = [config]
    if not isinstance(entries, list):
        raise ValueError("Task config field 'tasks' must be a list when provided.")

    tasks: list[ProofTask] = []
    defaults = config.get("metadata_defaults")
    if defaults is not None and not isinstance(defaults, dict):
        raise ValueError("Task config field 'metadata_defaults' must be an object when provided.")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("Each task config entry must be an object.")
        source_text = _inline_source_from_entry(entry)
        if source_text is None:
            continue
        metadata_defaults = dict(defaults or {})
        entry_metadata = entry.get("metadata")
        if entry_metadata is not None:
            if not isinstance(entry_metadata, dict):
                raise ValueError("Task config entry field 'metadata' must be an object.")
            metadata_defaults.update(entry_metadata)
        metadata_defaults.setdefault("task_config_file", getattr(args, "_task_config_path", None))
        metadata_defaults.setdefault("task_config_index", index)
        metadata_defaults.setdefault("task_source_kind", "inline")
        builder_with_metadata = LeanTaskBuilder(
            TaskBuilderConfig(
                hole_marker=args.hole_marker,
                inactive_hole_fill=args.inactive_hole_fill,
                default_split=args.split or builder.config.default_split,
                allowed_retrieval_scope=builder.config.allowed_retrieval_scope,
                metadata_defaults=metadata_defaults,
                allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
                allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
            )
        )
        entry_tasks = builder_with_metadata.build_from_source(
            source_text,
            source_path=entry.get("source_name") or entry.get("name") or f"task_config:{index}",
            split=entry.get("split") or args.split,
            task_id_prefix=entry.get("task_id_prefix") or entry.get("task_id") or entry.get("name"),
        )
        imports = _config_imports(config, entry)
        tasks.extend(_task_with_imports(task, imports) for task in entry_tasks)
    return tasks


def _inline_source_from_entry(entry: dict[str, Any]) -> str | None:
    for key in ("proof_source", "source_template", "lean"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def _config_imports(config: dict[str, Any], entry: dict[str, Any]) -> tuple[str, ...]:
    imports: list[str] = []
    for value in (config.get("imports"), entry.get("imports")):
        if value is None:
            continue
        if isinstance(value, str):
            imports.append(value)
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            imports.extend(value)
            continue
        raise ValueError("Task config imports must be a string or list of strings.")
    return tuple(dict.fromkeys(imports))


def _task_with_imports(task: ProofTask, imports: tuple[str, ...]) -> ProofTask:
    if not imports:
        return task
    return ProofTask(
        task_id=task.task_id,
        source_template=task.source_template,
        hole_marker=task.hole_marker,
        imports=tuple(dict.fromkeys((*task.imports, *imports))),
        metadata=task.metadata,
    )


def select_task(
    tasks: list[ProofTask],
    *,
    task_id: str | None = None,
    task_index: int = 0,
) -> ProofTask:
    if task_id is not None:
        for task in tasks:
            if task.task_id == task_id:
                return task
        raise ValueError(f"Task id not found: {task_id}")
    if task_index < 0 or task_index >= len(tasks):
        raise ValueError(f"Task index {task_index} is out of range for {len(tasks)} tasks.")
    return tasks[task_index]


def _require_source(args: Namespace) -> str:
    if args.source is None:
        raise ValueError("Provide a Lean source path or --task-config with a source field.")
    return args.source
