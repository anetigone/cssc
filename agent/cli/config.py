"""Task config merging for the CLI.

A task config is a JSON object whose keys mirror the CLI argument names
(without the leading ``--``). Only the keys listed in :data:`CONFIG_FIELDS`
are honored; any other key is rejected so typos surface immediately.
"""

from __future__ import annotations

import json
from argparse import Namespace
from typing import Any

from agent.input.parsing import (
    INFORMAL_PROOF_KEYS,
    LEAN_SOURCE_KEYS,
    PROBLEM_KEYS,
    config_informal_proof as _config_informal_proof,
    config_lean_source as _config_lean_source,
    config_problem as _config_problem,
    config_value as _config_value,
    has_inline_source as _has_inline_source,
    has_nl_source as _has_nl_source,
)

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
        "formalization_cache_dir",
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


def _has_any_source(config: dict[str, Any]) -> bool:
    return _has_inline_source(config) or _has_nl_source(config)
