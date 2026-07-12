"""Alignment between mathematical steps and Lean declarations/goals.

A mathematical step must be linkable to the Lean declaration or checker goal
that realizes it, because error attribution needs to answer "which Lean
declaration does this step implement, and which step does a failing checker
goal correspond to?". When the mapping cannot be established precisely, the
link is recorded as ``UNALIGNED`` rather than faked — the design principle is
to surface uncertain attribution, not disguise it as a definite classification.

This layer is proof-system-neutral data: it carries only identifiers (argument
step id, Lean declaration id, source span, goal fingerprint) and a relation.
It does not itself derive the links; that is the model's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AlignmentRelation(str, Enum):
    """How strongly an argument step maps to a Lean target."""

    IMPLEMENTS = "implements"
    PARTIAL = "partial"
    UNALIGNED = "unaligned"


@dataclass(frozen=True)
class AlignmentLink:
    """One mapping from an argument step to a Lean declaration and/or goal."""

    argument_step_id: str
    lean_declaration_id: str | None = None
    source_span: tuple[int, int] | None = None
    goal_fingerprint: str | None = None
    relation: AlignmentRelation = AlignmentRelation.UNALIGNED

    def to_dict(self) -> dict[str, Any]:
        return {
            "argument_step_id": self.argument_step_id,
            "lean_declaration_id": self.lean_declaration_id,
            "source_span": list(self.source_span) if self.source_span else None,
            "goal_fingerprint": self.goal_fingerprint,
            "relation": self.relation.value,
        }


def alignment_link_from_dict(data: dict[str, Any]) -> AlignmentLink:
    span = data.get("source_span")
    return AlignmentLink(
        argument_step_id=data["argument_step_id"],
        lean_declaration_id=data.get("lean_declaration_id"),
        source_span=tuple(span) if span else None,
        goal_fingerprint=data.get("goal_fingerprint"),
        relation=AlignmentRelation(
            data.get("relation", AlignmentRelation.UNALIGNED.value)
        ),
    )
