"""Stable JSON artifacts exchanged between CLI stages."""

from __future__ import annotations

from typing import Any

from agent.agents import FormalizationResult
from agent.tasks.types import TaskInputSpec


ARTIFACT_SCHEMA_VERSION = 1
FORMALIZATION_ARTIFACT = "cssc.formalization"


def formalization_artifact(
    spec: TaskInputSpec,
    result: FormalizationResult,
    *,
    hole_marker: str,
) -> dict[str, Any]:
    """Return a task-config-compatible formalization artifact."""
    metadata = {
        **spec.metadata,
        **result.metadata,
        "natural_language_problem": spec.text,
        "input_kind": "natural_language",
    }
    if result.natural_language_proof:
        metadata["natural_language_proof"] = result.natural_language_proof
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_type": FORMALIZATION_ARTIFACT,
        "task_id": spec.task_id,
        "input_kind": "lean",
        "source_template": result.proof_source,
        "hole_marker": hole_marker,
        "metadata": metadata,
    }


def formalization_payload(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a task-config-compatible artifact collection."""
    return {
        "ok": True,
        "stage": "formalize",
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_type": f"{FORMALIZATION_ARTIFACT}.collection",
        "tasks": results,
    }
