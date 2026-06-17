"""Namespace-free helpers for parsing task configs and input entries."""

from __future__ import annotations

from typing import Any, Iterator


# Canonical aliases for natural-language problem/proof fields in task configs.
# These are shared by config validation, task building, and formalization setup
# so the key list is defined in exactly one place.
PROBLEM_KEYS: tuple[str, ...] = ("problem", "problem_statement", "natural_language_problem")
INFORMAL_PROOF_KEYS: tuple[str, ...] = ("informal_proof", "natural_language_proof")
LEAN_SOURCE_KEYS: tuple[str, ...] = ("proof_source", "source_template", "lean")


def config_value(entry: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first string value among ``keys`` in ``entry``, if any."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def config_problem(entry: dict[str, Any]) -> str | None:
    """Return the natural-language problem text from a config entry."""
    return config_value(entry, PROBLEM_KEYS)


def config_informal_proof(entry: dict[str, Any]) -> str | None:
    """Return the informal proof text from a config entry."""
    return config_value(entry, INFORMAL_PROOF_KEYS)


def config_lean_source(entry: dict[str, Any]) -> str | None:
    """Return the inline Lean source text from a config entry."""
    return config_value(entry, LEAN_SOURCE_KEYS)


def has_inline_source(config: dict[str, Any]) -> bool:
    """Return True if ``config`` (or any entry in its ``tasks`` list) has inline Lean source."""
    if config_lean_source(config) is not None:
        return True
    tasks = config.get("tasks")
    return isinstance(tasks, list) and any(
        isinstance(item, dict) and config_lean_source(item) is not None for item in tasks
    )


def has_nl_source(config: dict[str, Any]) -> bool:
    """Return True if ``config`` (or any entry in its ``tasks`` list) has a natural-language problem."""
    if config_problem(config) is not None:
        return True
    tasks = config.get("tasks")
    if not isinstance(tasks, list):
        return False
    return any(isinstance(item, dict) and config_problem(item) is not None for item in tasks)


def iter_task_config_entries(
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


def copy_text_field(
    target: dict[str, Any],
    source: dict[str, Any],
    aliases: tuple[str, ...],
    target_key: str,
) -> None:
    """Copy the first string alias value from ``source`` to ``target[target_key]``."""
    for key in aliases:
        value = source.get(key)
        if isinstance(value, str):
            target[target_key] = value
            return


def config_imports(config: dict[str, Any], entry: dict[str, Any]) -> tuple[str, ...]:
    """Return the merged imports tuple from ``config`` and ``entry``.

    Each may be a string or a list of strings.
    """
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
