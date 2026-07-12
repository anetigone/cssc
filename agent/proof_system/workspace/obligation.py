"""Proof obligation primitive and lifecycle status."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ObligationStatus(str, Enum):
    """Lifecycle state of one proof obligation."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    # A previous version that a newer version of the same obligation superseded.
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ProofObligation:
    """One proof obligation: *what* to prove plus its dependencies.

    An obligation only defines the statement and its dependency edges; concrete
    mathematical arguments and Lean implementations are search-branch concerns
    and are not carried here.

    Versioning rule: when the statement, assumptions or dependencies change, a
    new :class:`ProofObligation` instance is created with a bumped ``version``
    and the same ``obligation_id``; the previous instance is marked
    :attr:`ObligationStatus.SUPERSEDED`. Old Lean artifacts must never be
    silently reattached to a new version.
    """

    obligation_id: str
    version: int
    title: str = ""

    statement_nl: str = ""
    lean_statement: str = ""
    assumptions: tuple[str, ...] = ()

    dependency_ids: tuple[str, ...] = ()
    rationale: str = ""
    required_capabilities: tuple[str, ...] = ()

    status: ObligationStatus = ObligationStatus.OPEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "version": self.version,
            "title": self.title,
            "statement_nl": self.statement_nl,
            "lean_statement": self.lean_statement,
            "assumptions": list(self.assumptions),
            "dependency_ids": list(self.dependency_ids),
            "rationale": self.rationale,
            "required_capabilities": list(self.required_capabilities),
            "status": self.status.value,
        }


def obligation_from_dict(data: dict[str, Any]) -> ProofObligation:
    return ProofObligation(
        obligation_id=data["obligation_id"],
        version=int(data["version"]),
        title=data.get("title", ""),
        statement_nl=data.get("statement_nl", ""),
        lean_statement=data.get("lean_statement", ""),
        assumptions=tuple(data.get("assumptions", ())),
        dependency_ids=tuple(data.get("dependency_ids", ())),
        rationale=data.get("rationale", ""),
        required_capabilities=tuple(data.get("required_capabilities", ())),
        status=ObligationStatus(data.get("status", ObligationStatus.OPEN.value)),
    )
