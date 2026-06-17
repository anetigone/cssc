"""Task config merging for the CLI.

A task config is a JSON object whose keys mirror the CLI argument names
(without the leading ``--``). Only the keys listed in :data:`CONFIG_FIELDS`
are honored; any other key is rejected so typos surface immediately.
"""

from __future__ import annotations

import json
from argparse import Namespace
from typing import Any

from .paths import resolve_agent_path, resolve_agent_root


# Keys permitted in a task config. Each maps 1:1 to the argparse destination
# of the same name and is written onto the parsed args namespace.
CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "source",
        "project_root",
        "split",
        "pattern",
        "task_id",
        "task_index",
        "hole_marker",
        "inactive_hole_fill",
        "allow_multiple_marker_tasks",
        "allow_multiple_sorry_tasks",
        "enable_retrieval",
        "retrieval_source",
        "max_retrieval_results",
        "retrieve_before_first_model_call",
        "input_kind",
        "problem_file",
    }
)

# Config-only structural fields consumed by the task builder directly from
# the config object rather than through CLI args.
STRUCTURAL_FIELDS: frozenset[str] = frozenset(
    {
        "imports",
        "tasks",
        "problem",
        "problem_statement",
        "natural_language_problem",
        "informal_proof",
        "natural_language_proof",
        "proof_source",
        "source_template",
        "lean",
    }
)

# Canonical aliases for natural-language problem/proof fields in task configs.
# These are shared by config validation, task building, and formalization setup
# so the key list is defined in exactly one place.
PROBLEM_KEYS: tuple[str, ...] = ("problem", "problem_statement", "natural_language_problem")
INFORMAL_PROOF_KEYS: tuple[str, ...] = ("informal_proof", "natural_language_proof")
LEAN_SOURCE_KEYS: tuple[str, ...] = ("proof_source", "source_template", "lean")


def apply_task_config(args: Namespace) -> Namespace:
    if not args.task_config:
        return args

    agent_root = resolve_agent_root(args.agent_root)
    config_path = resolve_agent_path(agent_root, args.task_config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"Task config must be a JSON object: {config_path}")
    setattr(args, "_task_config_data", config)
    setattr(args, "_task_config_path", str(config_path))

    unknown = config.keys() - CONFIG_FIELDS - STRUCTURAL_FIELDS
    if unknown:
        raise ValueError(
            f"Task config {config_path} has unknown field(s): "
            f"{', '.join(sorted(unknown))}. "
            f"Allowed fields: {', '.join(sorted(CONFIG_FIELDS | STRUCTURAL_FIELDS))}."
        )

    for key, value in config.items():
        if key == "retrieval_source":
            setattr(args, key, _coerce_retrieval_source(value))
        else:
            setattr(args, key, value)

    if args.source is None and not _has_any_source(config):
        raise ValueError(f"Task config {config_path} does not define source/problem, and no source was provided.")
    return args


def _coerce_retrieval_source(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("retrieval_source must be a string or list of strings.")


def _config_value(entry: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first string value among ``keys`` in ``entry``, if any."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def _config_problem(entry: dict[str, Any]) -> str | None:
    """Return the natural-language problem text from a config entry."""
    return _config_value(entry, PROBLEM_KEYS)


def _config_informal_proof(entry: dict[str, Any]) -> str | None:
    """Return the informal proof text from a config entry."""
    return _config_value(entry, INFORMAL_PROOF_KEYS)


def _config_lean_source(entry: dict[str, Any]) -> str | None:
    """Return the inline Lean source text from a config entry."""
    return _config_value(entry, LEAN_SOURCE_KEYS)


def _has_inline_source(config: dict[str, Any]) -> bool:
    if _config_lean_source(config) is not None:
        return True
    tasks = config.get("tasks")
    return isinstance(tasks, list) and any(
        isinstance(item, dict) and _config_lean_source(item) is not None for item in tasks
    )


def _has_nl_source(config: dict[str, Any]) -> bool:
    if _config_problem(config) is not None:
        return True
    tasks = config.get("tasks")
    if not isinstance(tasks, list):
        return False
    return any(isinstance(item, dict) and _config_problem(item) is not None for item in tasks)


def _has_any_source(config: dict[str, Any]) -> bool:
    return _has_inline_source(config) or _has_nl_source(config)

