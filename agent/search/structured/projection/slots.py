"""Prompt-renderable slot dataclasses for the structured context projection.

Each slot is a frozen value object with ``to_dict`` / ``from_dict`` helpers so
that the projection can cross the structured→prompt boundary as a plain dict.
No workspace logic lives here; see :mod:`.core` for the actual projection
builder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Most recent observations kept after dedup. Observations accumulate across
#: attempts, so the tail (latest) is the most relevant for a repair attempt.
MAX_PROJECTED_OBSERVATIONS = 12

#: Sibling strategies on the same obligation to surface. Bounded purely to
#: keep the prompt from listing an unbounded fan-out of repair branches.
MAX_SIBLING_BRANCHES = 8


@dataclass(frozen=True)
class ObligationSlot:
    """One obligation as seen by the context projection."""

    obligation_id: str
    version: int
    title: str
    statement_nl: str
    lean_statement: str
    is_root: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "version": self.version,
            "title": self.title,
            "statement_nl": self.statement_nl,
            "lean_statement": self.lean_statement,
            "is_root": self.is_root,
        }


def obligation_slot_from_dict(data: dict[str, Any]) -> ObligationSlot:
    return ObligationSlot(
        obligation_id=data["obligation_id"],
        version=int(data["version"]),
        title=data.get("title", ""),
        statement_nl=data.get("statement_nl", ""),
        lean_statement=data.get("lean_statement", ""),
        is_root=bool(data.get("is_root", False)),
    )


@dataclass(frozen=True)
class DependencyFact:
    """One obligation in the current obligation's dependency closure.

    ``has_accepted_fact`` is ``False`` when the helper obligation has not yet
    been proven (or only by a stale version), so the prompt can mark it as an
    open dependency rather than a reusable conclusion. ``declaration_id`` names
    the helper's Lean declaration when it has been proven, so a parent proof can
    reuse it by name; it is ``None`` for an open dependency.
    """

    obligation_id: str
    obligation_version: int
    statement: str
    has_accepted_fact: bool
    declaration_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "statement": self.statement,
            "has_accepted_fact": self.has_accepted_fact,
            "declaration_id": self.declaration_id,
        }


def dependency_fact_from_dict(data: dict[str, Any]) -> DependencyFact:
    declaration_id = data.get("declaration_id")
    return DependencyFact(
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        statement=data.get("statement", ""),
        has_accepted_fact=bool(data.get("has_accepted_fact", False)),
        declaration_id=declaration_id if isinstance(declaration_id, str) else None,
    )


@dataclass(frozen=True)
class AcceptedFactSlot:
    """One reusable accepted fact.

    ``declaration_id`` names the fact's Lean declaration when available, so a
    dependent obligation's prompt can refer to the helper by name rather than
    only by obligation id.
    """

    obligation_id: str
    statement: str
    declaration_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "statement": self.statement,
            "declaration_id": self.declaration_id,
        }


def accepted_fact_slot_from_dict(data: dict[str, Any]) -> AcceptedFactSlot:
    declaration_id = data.get("declaration_id")
    return AcceptedFactSlot(
        obligation_id=data["obligation_id"],
        statement=data.get("statement", ""),
        declaration_id=declaration_id if isinstance(declaration_id, str) else None,
    )


@dataclass(frozen=True)
class ArgumentStepSlot:
    """One argument step plus its goal↔Lean alignment relation."""

    step_id: str
    claim: str
    justification: str
    depends_on: tuple[str, ...]
    #: ``"implements"`` / ``"partial"`` / ``"unaligned"``; ``None`` when no
    #: alignment link exists for this step.
    alignment_relation: str | None
    aligned_declaration: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "claim": self.claim,
            "justification": self.justification,
            "depends_on": list(self.depends_on),
            "alignment_relation": self.alignment_relation,
            "aligned_declaration": self.aligned_declaration,
        }


def argument_step_slot_from_dict(data: dict[str, Any]) -> ArgumentStepSlot:
    relation = data.get("alignment_relation")
    return ArgumentStepSlot(
        step_id=data["step_id"],
        claim=data.get("claim", ""),
        justification=data.get("justification", ""),
        depends_on=tuple(data.get("depends_on", ())),
        alignment_relation=relation if isinstance(relation, str) else None,
        aligned_declaration=data.get("aligned_declaration"),
    )


@dataclass(frozen=True)
class ObservationSlot:
    """One deduplicated observation."""

    observation_id: str
    source: str
    category: str
    message: str
    goal_fingerprint: str | None
    raw_evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source": self.source,
            "category": self.category,
            "message": self.message,
            "goal_fingerprint": self.goal_fingerprint,
            "raw_evidence_ref": self.raw_evidence_ref,
        }


def observation_slot_from_dict(data: dict[str, Any]) -> ObservationSlot:
    fingerprint = data.get("goal_fingerprint")
    return ObservationSlot(
        observation_id=data["observation_id"],
        source=data.get("source", ""),
        category=data.get("category", ""),
        message=data.get("message", ""),
        goal_fingerprint=fingerprint if isinstance(fingerprint, str) else None,
        raw_evidence_ref=data.get("raw_evidence_ref", ""),
    )


@dataclass(frozen=True)
class FailureHypothesisSlot:
    """One competing failure hypothesis."""

    hypothesis_id: str
    kind: str
    confidence: float
    evidence_ids: tuple[str, ...]
    affected_step_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "kind": self.kind,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "affected_step_ids": list(self.affected_step_ids),
        }


def failure_hypothesis_slot_from_dict(
    data: dict[str, Any],
) -> FailureHypothesisSlot:
    return FailureHypothesisSlot(
        hypothesis_id=data["hypothesis_id"],
        kind=data.get("kind", ""),
        confidence=float(data.get("confidence", 0.0)),
        evidence_ids=tuple(data.get("evidence_ids", ())),
        affected_step_ids=tuple(data.get("affected_step_ids", ())),
    )


@dataclass(frozen=True)
class SiblingBranchSlot:
    """A short status snapshot of another strategy on the same obligation."""

    branch_id: str
    status: str
    has_artifact: bool
    observation_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "status": self.status,
            "has_artifact": self.has_artifact,
            "observation_count": self.observation_count,
        }


def sibling_branch_slot_from_dict(data: dict[str, Any]) -> SiblingBranchSlot:
    return SiblingBranchSlot(
        branch_id=data["branch_id"],
        status=data.get("status", ""),
        has_artifact=bool(data.get("has_artifact", False)),
        observation_count=int(data.get("observation_count", 0)),
    )
