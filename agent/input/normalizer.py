"""Namespace-free input normalization for Lean proof tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tasks.task_builder import LeanTaskBuilder, TaskBuildError, TaskBuilderConfig

if TYPE_CHECKING:
    from ..agents import FormalizationAgent
from ..tasks.types import ProofTask, TaskInputKind, TaskInputSpec
from .parsing import (
    INFORMAL_PROOF_KEYS,
    PROBLEM_KEYS,
    config_imports,
    config_informal_proof,
    config_lean_source,
    config_problem,
    copy_text_field,
    has_inline_source,
    has_nl_source,
    iter_task_config_entries,
)


@dataclass(frozen=True)
class NormalizedInput:
    """A thin wrapper around normalized task input specs."""

    kind: TaskInputKind
    specs: tuple[TaskInputSpec, ...]
    task_config_path: str | None = None
    metadata_defaults: dict[str, Any] = field(default_factory=dict)


class InputNormalizer:
    """Convert raw CLI/library input into a Namespace-free normalized form."""

    def resolve_kind(
        self,
        *,
        config: dict[str, Any] | None = None,
        input_kind: str = "auto",
        problem: str | None = None,
        problem_file: str | None = None,
        source: str | None = None,
    ) -> TaskInputKind:
        """Return the resolved input kind, matching the legacy resolution order."""
        if isinstance(config, dict):
            if has_inline_source(config):
                return TaskInputKind.LEAN
            if has_nl_source(config):
                return TaskInputKind.NATURAL_LANGUAGE

        if input_kind != "auto":
            return TaskInputKind(input_kind)

        if problem or problem_file:
            return TaskInputKind.NATURAL_LANGUAGE

        if isinstance(source, str) and Path(source).suffix.lower() in {".txt", ".md", ".tex"}:
            return TaskInputKind.NATURAL_LANGUAGE

        return TaskInputKind.LEAN

    def normalize(
        self,
        *,
        source: str | None = None,
        problem: str | None = None,
        problem_file: str | None = None,
        input_kind: str = "auto",
        task_config: dict[str, Any] | None = None,
        task_config_path: str | None = None,
        agent_root: str | Path = ".",
        pattern: str = "*.lean",
        split: str | None = None,
        metadata_defaults: dict[str, Any] | None = None,
    ) -> NormalizedInput:
        """Normalize raw input into ``TaskInputSpec`` objects."""
        if isinstance(task_config, dict):
            return self._normalize_from_config(
                task_config,
                task_config_path=task_config_path,
                input_kind=input_kind,
                split=split,
            )
        return self._normalize_direct(
            source=source,
            problem=problem,
            problem_file=problem_file,
            input_kind=input_kind,
            agent_root=agent_root,
            pattern=pattern,
            split=split,
        )

    def _normalize_from_config(
        self,
        config: dict[str, Any],
        *,
        task_config_path: str | None,
        input_kind: str,
        split: str | None,
    ) -> NormalizedInput:
        kind = self.resolve_kind(config=config, input_kind=input_kind)
        specs: list[TaskInputSpec] = []
        for index, entry, metadata in iter_task_config_entries(config):
            task_id = str(entry.get("task_id") or entry.get("name") or f"task_config:{index}")
            source_name = str(entry.get("source_name") or entry.get("name") or f"task_config:{index}")
            entry_metadata = dict(metadata)
            entry_metadata.setdefault("task_config_file", task_config_path)
            entry_metadata.setdefault("task_config_index", index)
            copy_text_field(entry_metadata, config, PROBLEM_KEYS, "natural_language_problem")
            copy_text_field(entry_metadata, entry, PROBLEM_KEYS, "natural_language_problem")
            copy_text_field(entry_metadata, config, INFORMAL_PROOF_KEYS, "natural_language_proof")
            copy_text_field(entry_metadata, entry, INFORMAL_PROOF_KEYS, "natural_language_proof")
            if kind == TaskInputKind.LEAN:
                text = config_lean_source(entry)
                if text is None:
                    continue
                entry_metadata.setdefault("task_source_kind", "inline")
                specs.append(
                    TaskInputSpec(
                        task_id=task_id,
                        kind=TaskInputKind.LEAN,
                        text=text,
                        source_name=source_name,
                        imports=config_imports(config, entry),
                        metadata=entry_metadata,
                        split=entry.get("split") or split,
                    )
                )
            else:
                text = config_problem(entry) or config_problem(config)
                if text is None:
                    continue
                entry_metadata.setdefault("task_source_kind", "natural_language")
                specs.append(
                    TaskInputSpec(
                        task_id=task_id,
                        kind=TaskInputKind.NATURAL_LANGUAGE,
                        text=text,
                        source_name=source_name,
                        imports=config_imports(config, entry),
                        metadata=entry_metadata,
                        informal_proof=config_informal_proof(entry) or config_informal_proof(config),
                        split=entry.get("split") or split,
                    )
                )
        return NormalizedInput(
            kind=kind,
            specs=tuple(specs),
            task_config_path=task_config_path,
            metadata_defaults=config.get("metadata_defaults") or {},
        )

    def _normalize_direct(
        self,
        *,
        source: str | None,
        problem: str | None,
        problem_file: str | None,
        input_kind: str,
        agent_root: str | Path,
        pattern: str,
        split: str | None,
    ) -> NormalizedInput:
        kind = self.resolve_kind(
            input_kind=input_kind,
            problem=problem,
            problem_file=problem_file,
            source=source,
        )
        if kind == TaskInputKind.NATURAL_LANGUAGE:
            text = problem
            source_name: str | None = problem_file or source or "cli:problem"
            if text is None and problem_file:
                problem_path = Path(problem_file)
                if not problem_path.is_absolute():
                    problem_path = Path(agent_root) / problem_path
                text = problem_path.resolve().read_text(encoding="utf-8")
            if text is None and source:
                source_path = Path(source)
                if not source_path.is_absolute():
                    source_path = Path(agent_root) / source_path
                text = source_path.resolve().read_text(encoding="utf-8")
            if not text:
                raise ValueError(
                    "Provide a natural-language problem via --problem/--problem-file or a source path."
                )
            specs = (
                TaskInputSpec(
                    task_id="natural_language_task",
                    kind=TaskInputKind.NATURAL_LANGUAGE,
                    text=text,
                    source_name=source_name,
                    metadata={"task_source_kind": "natural_language"},
                    split=split,
                ),
            )
            return NormalizedInput(kind=kind, specs=specs)

        if source is None:
            raise ValueError("Provide a Lean source path or --task-config with a source field.")
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = Path(agent_root) / source_path
        source_path = source_path.resolve()
        is_directory = source_path.is_dir()
        specs = (
            TaskInputSpec(
                task_id=source_path.stem,
                kind=TaskInputKind.LEAN,
                text="",
                source_name=str(source_path),
                source_path=str(source_path),
                is_directory=is_directory,
                directory_pattern=pattern,
                metadata={"task_source_kind": "file" if not is_directory else "directory"},
                split=split,
            ),
        )
        return NormalizedInput(kind=kind, specs=specs)


def prepare_tasks(
    normalized: NormalizedInput,
    *,
    builder: LeanTaskBuilder,
    formalizer: FormalizationAgent | None = None,
) -> list[ProofTask]:
    """Build ``ProofTask`` objects from normalized input.

    Natural-language specs are formalized first (if a formalizer is provided),
    then converted to tasks by the builder. Lean specs are passed directly to
    the builder.
    """
    from ..agents import FormalizationRequest

    tasks: list[ProofTask] = []
    for spec in normalized.specs:
        if spec.kind == TaskInputKind.NATURAL_LANGUAGE:
            if formalizer is None:
                raise TaskBuildError("Natural-language task input requires a formalization agent.")
            request = FormalizationRequest(
                problem=spec.text,
                task_id=spec.task_id,
                imports=spec.imports,
                informal_proof=spec.informal_proof,
                context=spec.context,
                hole_marker=builder.config.hole_marker,
                metadata=spec.metadata,
            )
            result = formalizer.formalize(request)
            task_metadata = {
                **spec.metadata,
                **result.metadata,
                "formalized_by": result.metadata.get("model", "formalization_agent"),
                "natural_language_problem": spec.text,
                "input_kind": TaskInputKind.NATURAL_LANGUAGE.value,
            }
            if result.natural_language_proof:
                task_metadata["natural_language_proof"] = result.natural_language_proof
            entry_builder = _builder_with_metadata(builder, task_metadata)
            entry_tasks = entry_builder.build_from_source(
                result.proof_source,
                source_path=spec.source_name or spec.task_id,
                split=spec.split,
                task_id_prefix=spec.task_id,
            )
        elif spec.kind == TaskInputKind.LEAN:
            task_metadata = dict(spec.metadata)
            task_metadata.setdefault("input_kind", TaskInputKind.LEAN.value)
            task_metadata.setdefault("task_config_file", normalized.task_config_path)
            entry_builder = _builder_with_metadata(builder, task_metadata)
            if spec.is_directory:
                assert spec.source_path is not None
                entry_tasks = entry_builder.build_from_directory(
                    spec.source_path,
                    split=spec.split,
                    pattern=spec.directory_pattern,
                )
            elif spec.source_path:
                entry_tasks = entry_builder.build_from_file(
                    spec.source_path,
                    split=spec.split,
                )
            else:
                entry_tasks = entry_builder.build_from_source(
                    spec.text,
                    source_path=spec.source_name or spec.task_id,
                    split=spec.split,
                    task_id_prefix=spec.task_id,
                )
        else:
            raise TaskBuildError(f"Unknown task input kind: {spec.kind}")
        tasks.extend(_tasks_with_imports(entry_tasks, spec.imports))
    if not tasks:
        raise TaskBuildError("No tasks were extracted from task input.")
    return tasks


def _builder_with_metadata(
    builder: LeanTaskBuilder,
    metadata: dict[str, Any],
) -> LeanTaskBuilder:
    """Return a new builder whose config carries ``metadata`` as defaults."""
    return LeanTaskBuilder(
        TaskBuilderConfig(
            hole_marker=builder.config.hole_marker,
            inactive_hole_fill=builder.config.inactive_hole_fill,
            default_split=builder.config.default_split,
            allowed_retrieval_scope=builder.config.allowed_retrieval_scope,
            metadata_defaults=metadata,
            allow_multiple_marker_tasks=builder.config.allow_multiple_marker_tasks,
            allow_multiple_sorry_tasks=builder.config.allow_multiple_sorry_tasks,
        )
    )


def _tasks_with_imports(tasks: list[ProofTask], imports: tuple[str, ...]) -> list[ProofTask]:
    if not imports:
        return tasks
    return [_task_with_imports(task, imports) for task in tasks]


def _task_with_imports(task: ProofTask, imports: tuple[str, ...]) -> ProofTask:
    return ProofTask(
        task_id=task.task_id,
        source_template=task.source_template,
        hole_marker=task.hole_marker,
        imports=tuple(dict.fromkeys((*task.imports, *imports))),
        input_kind=task.input_kind,
        metadata=task.metadata,
    )
