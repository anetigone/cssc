"""Structured proof-search state primitives.

Phase 3 introduces the authoritative state for ``structured`` execution mode:

* :class:`ProofObligation` — one proof obligation with versioning and dependencies;
* :class:`ObligationGraph` — the acyclic dependency DAG of obligations for a run;
* :class:`ProofWorkspace` (added in a later commit) — the top-level container.

These are proof-system-neutral frozen dataclasses, sibling to
:class:`ProofTask`, :class:`CheckResult` and :class:`GoalState` in this module.
They carry the structured layer's *what to prove* and *how obligations depend*;
the *how to prove* (branches, argument steps, Lean artifacts) belongs to
Phase 4+. Each type round-trips through ``to_dict`` / ``from_dict`` so the
structured run can be persisted into the trace alongside the Phase 0 raw
fields.

The minimal loop never imports this module, so it pays no DAG cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Sequence


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

    Field shape follows the Phase 3 design note (``tmp/plan1.md`` §4). An
    obligation only defines the statement and its dependency edges; concrete
    mathematical arguments and Lean implementations are search-branch concerns
    (Phase 4+) and are not carried here.

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


@dataclass(frozen=True)
class ObligationGraph:
    """The acyclic dependency DAG of obligations for one run.

    Obligations are stored as a tuple keyed by ``obligation_id`` (the latest
    non-superseded version wins the id slot; superseded versions are retained
    for provenance but resolved only by explicit lookup). The graph owns a
    single root obligation that every other obligation must eventually depend
    on; decomposition in later commits may add auxiliary obligations.
    """

    obligations: tuple[ProofObligation, ...] = ()
    root_obligation_id: str = ""

    def by_id(self, obligation_id: str) -> ProofObligation | None:
        for obligation in self.obligations:
            if obligation.obligation_id == obligation_id:
                return obligation
        return None

    def root(self) -> ProofObligation | None:
        if not self.root_obligation_id:
            return None
        return self.by_id(self.root_obligation_id)

    def active(self) -> tuple[ProofObligation, ...]:
        """Non-superseded obligations."""
        return tuple(
            obligation
            for obligation in self.obligations
            if obligation.status != ObligationStatus.SUPERSEDED
        )

    def superseded(self) -> tuple[ProofObligation, ...]:
        return tuple(
            obligation
            for obligation in self.obligations
            if obligation.status == ObligationStatus.SUPERSEDED
        )

    def with_obligation(self, obligation: ProofObligation) -> ObligationGraph:
        """Return a new graph with ``obligation`` replacing any prior version.

        If an obligation with the same id already exists, it is superseded by
        being overwritten in the tuple; superseded versions are only kept when
        explicitly inserted via :meth:`with_obligations`.
        """
        others = tuple(
            obligation_
            for obligation_ in self.obligations
            if obligation_.obligation_id != obligation.obligation_id
        )
        return replace(self, obligations=(*others, obligation))

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_obligation_id": self.root_obligation_id,
            "obligations": [obligation.to_dict() for obligation in self.obligations],
        }


def obligation_graph_from_dict(data: dict[str, Any]) -> ObligationGraph:
    obligations = tuple(
        obligation_from_dict(item) for item in data.get("obligations", ())
    )
    return ObligationGraph(
        obligations=obligations,
        root_obligation_id=data.get("root_obligation_id", ""),
    )
