"""Task building and selection for the Lean task-solving CLI."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any, Iterator

from agent import LeanTaskBuilder, ProofTask, TaskBuildError, TaskBuilderConfig, TaskInputKind
from agent.agents import FormalizationAgent, FormalizationRequest

from .config import (
    INFORMAL_PROOF_KEYS,
    PROBLEM_KEYS,
    _has_inline_source,
    _has_nl_source,
    _config_lean_source,
    _config_problem,
)
from .paths import resolve_agent_path


def classify_input(args: Namespace, config: dict[str, Any] | None = None) -> TaskInputKind:
    """Return the resolved input kind for the current CLI arguments and config.

    The resolution order is intentionally a single source of truth shared by
    ``build_formalization_agent`` and ``build_tasks``:

    1. Task-config content is unambiguous: inline Lean source implies ``LEAN``,
       a natural-language problem implies ``NATURAL_LANGUAGE``.
    2. Explicit ``--input-kind`` (other than ``auto``) is honored.
    3. Direct natural-language input (``--problem`` / ``--problem-file``)
       implies ``NATURAL_LANGUAGE``.
    4. On ``auto``, certain source suffixes (``.txt``, ``.md``, ``.tex``)
       imply ``NATURAL_LANGUAGE``.
    5. Everything else defaults to ``LEAN``.
    """
    if config is None:
        config = getattr(args, "_task_config_data", None)
    if isinstance(config, dict):
        if _has_inline_source(config):
            return TaskInputKind.LEAN
        if _has_nl_source(config):
            return TaskInputKind.NATURAL_LANGUAGE

    raw = getattr(args, "input_kind", "auto")
    if raw != "auto":
        return TaskInputKind(raw)

    if _has_direct_nl_input(args):
        return TaskInputKind.NATURAL_LANGUAGE

    source = getattr(args, "source", None)
    if isinstance(source, str) and Path(source).suffix.lower() in {".txt", ".md", ".tex"}:
        return TaskInputKind.NATURAL_LANGUAGE

    return TaskInputKind.LEAN


def build_tasks(args: Namespace, *, formalizer: FormalizationAgent | None = None) -> list[ProofTask]:
    config = TaskBuilderConfig(
        hole_marker=args.hole_marker,
        inactive_hole_fill=args.inactive_hole_fill,
        allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
        allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
    )
    builder = LeanTaskBuilder(config)
    task_config = getattr(args, "_task_config_data", None)
    input_kind = classify_input(args, task_config)
    if isinstance(task_config, dict) and _has_inline_source(task_config):
        tasks = _build_tasks_from_config(builder, args, task_config)
    elif isinstance(task_config, dict) and _has_nl_source(task_config):
        tasks = _build_tasks_from_nl_config(builder, args, task_config, formalizer)
    elif input_kind == TaskInputKind.NATURAL_LANGUAGE:
        tasks = _build_tasks_from_nl_input(builder, args, formalizer)
    else:
        source = resolve_agent_path(Path(args.agent_root), require_source(args))
        if source.is_dir():
            tasks = builder.build_from_directory(source, split=args.split, pattern=args.pattern)
        else:
            tasks = builder.build_from_file(source, split=args.split)
    if not tasks:
        raise TaskBuildError("No tasks were extracted from task input.")
    return tasks


def _iter_task_config_entries(
    config: dict[str, Any],
) -> Iterator[tuple[int, dict[str, Any], dict[str, Any]]]:
    """Yield ``(index, entry, metadata_defaults)`` for each task config entry.

    Validates ``tasks``, ``metadata_defaults`` and per-entry ``metadata``.
    """
    entries = config.get("tasks")
    if entries is None:
        entries = [config]
    if not isinstance(entries, list):
        raise ValueError("Task config field 'tasks' must be a list when provided.")

    defaults = config.get("metadata_defaults")
    if defaults is not None and not isinstance(defaults, dict):
        raise ValueError("Task config field 'metadata_defaults' must be an object when provided.")

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("Each task config entry must be an object.")
        metadata = dict(defaults or {})
        entry_metadata = entry.get("metadata")
        if entry_metadata is not None:
            if not isinstance(entry_metadata, dict):
                raise ValueError("Task config entry field 'metadata' must be an object.")
            metadata.update(entry_metadata)
        yield index, entry, metadata


def _base_task_metadata(
    args: Namespace,
    index: int,
    defaults: dict[str, Any],
    entry: dict[str, Any],
    config: dict[str, Any] | None,
    task_source_kind: str,
) -> dict[str, Any]:
    """Return shared metadata for a task config entry."""
    metadata = dict(defaults)
    if config is not None:
        _copy_text_field(metadata, config, PROBLEM_KEYS, "natural_language_problem")
        _copy_text_field(metadata, config, INFORMAL_PROOF_KEYS, "natural_language_proof")
    _copy_text_field(metadata, entry, PROBLEM_KEYS, "natural_language_problem")
    _copy_text_field(metadata, entry, INFORMAL_PROOF_KEYS, "natural_language_proof")
    metadata.setdefault("task_config_file", getattr(args, "_task_config_path", None))
    metadata.setdefault("task_config_index", index)
    metadata.setdefault("task_source_kind", task_source_kind)
    return metadata


def _builder_with_metadata(
    builder: LeanTaskBuilder,
    args: Namespace,
    metadata: dict[str, Any],
) -> LeanTaskBuilder:
    """Return a new builder whose config carries ``metadata`` as defaults."""
    return LeanTaskBuilder(
        TaskBuilderConfig(
            hole_marker=args.hole_marker,
            inactive_hole_fill=args.inactive_hole_fill,
            default_split=args.split or builder.config.default_split,
            allowed_retrieval_scope=builder.config.allowed_retrieval_scope,
            metadata_defaults=metadata,
            allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
            allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
        )
    )


def _build_tasks_from_config(
    builder: LeanTaskBuilder,
    args: Namespace,
    config: dict[str, Any],
) -> list[ProofTask]:
    tasks: list[ProofTask] = []
    for index, entry, defaults in _iter_task_config_entries(config):
        source_text = _config_lean_source(entry)
        if source_text is None:
            continue
        metadata = _base_task_metadata(args, index, defaults, entry, config, "inline")
        entry_tasks = _builder_with_metadata(builder, args, metadata).build_from_source(
            source_text,
            source_path=entry.get("source_name") or entry.get("name") or f"task_config:{index}",
            split=entry.get("split") or args.split,
            task_id_prefix=entry.get("task_id_prefix") or entry.get("task_id") or entry.get("name"),
        )
        imports = _config_imports(config, entry)
        tasks.extend(_task_with_imports(task, imports) for task in entry_tasks)
    return tasks


def _build_tasks_from_nl_config(
    builder: LeanTaskBuilder,
    args: Namespace,
    config: dict[str, Any],
    formalizer: FormalizationAgent | None,
) -> list[ProofTask]:
    tasks: list[ProofTask] = []
    for index, entry, defaults in _iter_task_config_entries(config):
        problem = _config_problem(entry) or _config_problem(config)
        if problem is None:
            continue
        metadata = _base_task_metadata(args, index, defaults, entry, config, "natural_language")
        metadata["input_kind"] = TaskInputKind.NATURAL_LANGUAGE.value
        imports = _config_imports(config, entry)
        task_id = entry.get("task_id") or entry.get("name") or f"natural_language:{index}"
        source_name = entry.get("source_name") or entry.get("name") or f"natural_language:{index}"
        tasks.extend(
            _build_tasks_from_problem_text(
                builder,
                args,
                formalizer,
                problem=problem,
                source_name=str(source_name),
                task_id_prefix=str(task_id),
                imports=imports,
                metadata=metadata,
            )
        )
    return tasks


def _build_tasks_from_direct_problem(
    builder: LeanTaskBuilder,
    args: Namespace,
    formalizer: FormalizationAgent | None,
) -> list[ProofTask]:
    problem = _direct_problem_text(args)
    return _build_tasks_from_problem_text(
        builder,
        args,
        formalizer,
        problem=problem,
        source_name=getattr(args, "problem_file", None) or "cli:problem",
        task_id_prefix="natural_language_task",
        imports=(),
        metadata={"task_source_kind": "natural_language"},
    )


def _build_tasks_from_nl_input(
    builder: LeanTaskBuilder,
    args: Namespace,
    formalizer: FormalizationAgent | None,
) -> list[ProofTask]:
    if _has_direct_nl_input(args):
        return _build_tasks_from_direct_problem(builder, args, formalizer)
    source = resolve_agent_path(Path(args.agent_root), _require_nl_source(args))
    return _build_tasks_from_problem_text(
        builder,
        args,
        formalizer,
        problem=source.read_text(encoding="utf-8"),
        source_name=str(source),
        task_id_prefix=source.stem,
        imports=(),
        metadata={},
    )


def _build_tasks_from_problem_text(
    builder: LeanTaskBuilder,
    args: Namespace,
    formalizer: FormalizationAgent | None,
    *,
    problem: str,
    source_name: str,
    task_id_prefix: str,
    imports: tuple[str, ...],
    metadata: dict[str, Any],
) -> list[ProofTask]:
    if formalizer is None:
        raise TaskBuildError("Natural-language task input requires a formalization agent.")
    request = FormalizationRequest(
        problem=problem,
        task_id=task_id_prefix,
        imports=imports,
        informal_proof=_optional_str(metadata, "natural_language_proof"),
        hole_marker=args.hole_marker,
        metadata=metadata,
    )
    result = formalizer.formalize(request)
    task_metadata = {
        **metadata,
        **result.metadata,
        "formalized_by": result.metadata.get("model", "formalization_agent"),
        "natural_language_problem": problem,
        "input_kind": TaskInputKind.NATURAL_LANGUAGE.value,
    }
    if result.natural_language_proof:
        task_metadata["natural_language_proof"] = result.natural_language_proof
    entry_tasks = _builder_with_metadata(builder, args, task_metadata).build_from_source(
        result.proof_source,
        source_path=source_name,
        split=args.split,
        task_id_prefix=task_id_prefix,
    )
    return [_task_with_imports(task, imports) for task in entry_tasks]


def _copy_text_field(
    target: dict[str, Any],
    source: dict[str, Any],
    aliases: tuple[str, ...],
    target_key: str,
) -> None:
    for key in aliases:
        value = source.get(key)
        if isinstance(value, str):
            target[target_key] = value
            return


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
        input_kind=task.input_kind,
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


def require_source(args: Namespace) -> str:
    if args.source is None:
        raise ValueError("Provide a Lean source path or --task-config with a source field.")
    return args.source


def _require_nl_source(args: Namespace) -> str:
    if args.source is None:
        raise ValueError(
            "Provide a natural-language problem via --problem/--problem-file or a source path."
        )
    return args.source


def _has_direct_nl_input(args: Namespace) -> bool:
    return bool(getattr(args, "problem", None) or getattr(args, "problem_file", None))


def _direct_problem_text(args: Namespace) -> str:
    if getattr(args, "problem", None):
        return args.problem
    problem_file = getattr(args, "problem_file", None)
    if problem_file:
        path = resolve_agent_path(Path(args.agent_root), problem_file)
        return path.read_text(encoding="utf-8")
    raise ValueError("No natural-language problem was provided.")


def _optional_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None
