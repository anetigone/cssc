"""Task-level data structures shared by input builders and proof adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskInputKind(str, Enum):
    """User-facing task input modes."""

    AUTO = "auto"
    LEAN = "lean"
    NATURAL_LANGUAGE = "natural_language"


@dataclass(frozen=True)
class ProofTask:
    """A checker-ready proof-completion task with one active editable hole.

    Natural-language provenance and prompt context live in ``metadata`` under
    the keys ``natural_language_problem`` and ``natural_language_proof``. The
    verifier-facing target is still ``source_template`` plus ``hole_marker``.

    Multi-hole extraction still emits exactly one active hole per task. Later
    source-order tasks may contain explicit dependency markers recorded in
    ``metadata['dependency_markers']``; callers must materialize those markers
    with checker+safety accepted proofs before invoking a controller.
    """
    task_id: str
    source_template: str
    hole_marker: str = "{{proof}}"
    imports: tuple[str, ...] = ()
    input_kind: TaskInputKind = TaskInputKind.LEAN
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        metadata = dict(self.metadata)
        metadata.setdefault("input_kind", self.input_kind.value)
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True)
class TaskInputSpec:
    """Raw user task input before it becomes a checker-ready ``ProofTask``."""

    task_id: str
    kind: TaskInputKind
    text: str
    source_name: str | None = None
    imports: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # NEW: file-system provenance and directory scanning
    source_path: str | None = None
    is_directory: bool = False
    directory_pattern: str = "*.lean"
    # NEW: natural-language provenance passed to the formalizer
    informal_proof: str | None = None
    context: str | None = None
    # NEW: per-spec split override
    split: str | None = None
