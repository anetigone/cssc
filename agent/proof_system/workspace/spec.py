"""Workspace status and provenance-carrying specification objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class WorkspaceStatus(str, Enum):
    """Lifecycle state of a :class:`ProofWorkspace`."""

    INITIALIZING = "initializing"
    SEARCHING = "searching"
    ASSEMBLING = "assembling"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class FormalSpecification:
    """The fixed target of a structured run: original problem + Lean statement.

    Phase 3 only needs enough structure to seed the root obligation and let the
    final assembler rebuild the full source. NL↔Lean alignment and a richer
    specification (definitions, hypotheses, main goal) are Phase 4+ concerns;
    here it is a thin carrier of provenance.
    """

    statement_nl: str = ""
    lean_statement: str = ""
    source_task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_nl": self.statement_nl,
            "lean_statement": self.lean_statement,
            "source_task_id": self.source_task_id,
        }


def formal_specification_from_dict(data: dict[str, Any]) -> FormalSpecification:
    return FormalSpecification(
        statement_nl=data.get("statement_nl", ""),
        lean_statement=data.get("lean_statement", ""),
        source_task_id=data.get("source_task_id", ""),
    )


@dataclass(frozen=True)
class VerifiedFact:
    """A checker-verified conclusion reusable across branches.

    Provenance is mandatory: an accepted fact always carries the obligation
    version and the attempt that produced it, so a later revision of the
    obligation cannot silently reuse a stale verification.

    ``statement`` is what a dependent obligation's prompt reuses as the
    established conclusion. For a root obligation it is the proof body (the
    baseline behaviour); for a decomposed helper it is the helper's Lean
    declaration — the thing a parent proof references by name. ``declaration_id``
    names the Lean declaration a generator can call, and ``artifact_source``
    carries the rendered declaration text so a downstream projection can show
    the actual code without re-deriving it. Both default to ``None`` so existing
    single-root constructions stay unchanged.
    """

    obligation_id: str
    obligation_version: int
    statement: str
    source_attempt_index: int
    checker_category: str
    safety_accepted: bool
    declaration_id: str | None = None
    artifact_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "statement": self.statement,
            "source_attempt_index": self.source_attempt_index,
            "checker_category": self.checker_category,
            "safety_accepted": self.safety_accepted,
            "declaration_id": self.declaration_id,
            "artifact_source": self.artifact_source,
        }


def verified_fact_from_dict(data: dict[str, Any]) -> VerifiedFact:
    return VerifiedFact(
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        statement=data["statement"],
        source_attempt_index=int(data["source_attempt_index"]),
        checker_category=data["checker_category"],
        safety_accepted=bool(data["safety_accepted"]),
        declaration_id=data.get("declaration_id"),
        artifact_source=data.get("artifact_source"),
    )
