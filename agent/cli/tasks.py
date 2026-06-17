"""Task building and selection for the Lean task-solving CLI."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from agent import LeanTaskBuilder, ProofTask, TaskBuildError, TaskBuilderConfig, TaskInputKind
from agent.agents import FormalizationAgent
from agent.input.normalizer import InputNormalizer, prepare_tasks

from .paths import resolve_agent_path


def classify_input(args: Namespace, config: dict[str, Any] | None = None) -> TaskInputKind:
    """Return the resolved input kind for the current CLI arguments and config.

    This is a thin CLI adapter around ``InputNormalizer.resolve_kind``.
    """
    if config is None:
        config = getattr(args, "_task_config_data", None)
    return InputNormalizer().resolve_kind(
        config=config,
        input_kind=getattr(args, "input_kind", "auto"),
        problem=getattr(args, "problem", None),
        problem_file=getattr(args, "problem_file", None),
        source=getattr(args, "source", None),
    )


def build_tasks(args: Namespace, *, formalizer: FormalizationAgent | None = None) -> list[ProofTask]:
    """Build proof tasks from the parsed CLI arguments."""
    config = TaskBuilderConfig(
        hole_marker=args.hole_marker,
        inactive_hole_fill=args.inactive_hole_fill,
        allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
        allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
    )
    builder = LeanTaskBuilder(config)
    normalized = InputNormalizer().normalize(
        source=getattr(args, "source", None),
        problem=getattr(args, "problem", None),
        problem_file=getattr(args, "problem_file", None),
        input_kind=getattr(args, "input_kind", "auto"),
        task_config=getattr(args, "_task_config_data", None),
        task_config_path=getattr(args, "_task_config_path", None),
        agent_root=args.agent_root,
        pattern=args.pattern,
        split=args.split,
    )
    return prepare_tasks(normalized, builder=builder, formalizer=formalizer)


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
