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
class ObligationGraphReport:
    """Result of validating an :class:`ObligationGraph`.

    Validation is deterministic and never raises: any structural problem is
    reported in :attr:`errors` with ``ok`` set to ``False``. The controller
    decides how to react (e.g. refuse assembly); the validator only states
    facts, mirroring the Phase 0 principle of recording observations without
    inferring policy.
    """

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


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
        """Resolve an id to its latest (non-superseded) version.

        Superseded versions are retained for provenance but never returned by
        id lookup; use :meth:`superseded` to inspect history explicitly. When
        multiple versions exist, the highest ``version`` wins.
        """
        matches = [
            obligation
            for obligation in self.obligations
            if obligation.obligation_id == obligation_id
        ]
        if not matches:
            return None
        active = [o for o in matches if o.status != ObligationStatus.SUPERSEDED]
        pool = active or matches
        return max(pool, key=lambda obligation: obligation.version)

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

    def validate(self) -> ObligationGraphReport:
        """Check DAG invariants without raising.

        Verifies:

        * the root obligation exists and is not superseded;
        * every ``dependency_id`` refers to an obligation in the graph;
        * no active obligation depends on a superseded version;
        * the dependency edges form a DAG (no cycles);
        * every non-root active obligation reaches the root through its
          dependency closure.

        Returns a report; ``ok`` is ``True`` iff ``errors`` is empty.
        """
        errors: list[str] = []

        ids = {obligation.obligation_id for obligation in self.obligations}
        active_by_id = {
            obligation.obligation_id: obligation
            for obligation in self.obligations
            if obligation.status != ObligationStatus.SUPERSEDED
        }

        # Root presence and status.
        root = self.root()
        if root is None:
            errors.append(
                f"root obligation {self.root_obligation_id!r} is missing from the graph"
            )
        elif root.status == ObligationStatus.SUPERSEDED:
            errors.append(
                f"root obligation {self.root_obligation_id!r} is superseded"
            )

        # Dependency edges point to existing obligations and not to dead versions.
        for obligation in self.obligations:
            if obligation.status == ObligationStatus.SUPERSEDED:
                continue
            for dependency_id in obligation.dependency_ids:
                if dependency_id not in ids:
                    errors.append(
                        f"obligation {obligation.obligation_id!r} depends on "
                        f"missing obligation {dependency_id!r}"
                    )
                elif dependency_id not in active_by_id:
                    errors.append(
                        f"obligation {obligation.obligation_id!r} depends on "
                        f"superseded obligation {dependency_id!r}"
                    )

        # Acyclicity over the dependency edges of active obligations.
        cycle = _detect_cycle(active_by_id)
        if cycle is not None:
            errors.append(f"dependency cycle detected: {' -> '.join(cycle)}")

        # Every active non-root obligation reaches the root.
        if root is not None:
            reachable = _reverse_reachable(root.obligation_id, active_by_id)
            for obligation in active_by_id.values():
                if obligation.obligation_id == root.obligation_id:
                    continue
                if obligation.obligation_id not in reachable:
                    errors.append(
                        f"obligation {obligation.obligation_id!r} cannot reach "
                        f"root obligation {root.obligation_id!r}"
                    )

        return ObligationGraphReport(ok=not errors, errors=tuple(errors))

    def new_version(
        self,
        obligation_id: str,
        *,
        statement_nl: str | None = None,
        lean_statement: str | None = None,
        assumptions: tuple[str, ...] | None = None,
        dependency_ids: tuple[str, ...] | None = None,
        rationale: str | None = None,
    ) -> ObligationGraph:
        """Create the next version of an obligation.

        The previous instance is marked :attr:`ObligationStatus.SUPERSEDED`
        and retained for provenance; the new version carries any updated fields
        and starts ``OPEN``. Statement/assumption/dependency changes must go
        through this method so the graph keeps a full version history and the
        DAG invariant can be re-checked against the active set.
        """
        previous = self.by_id(obligation_id)
        if previous is None:
            raise KeyError(f"unknown obligation {obligation_id!r}")

        superseded = replace(previous, status=ObligationStatus.SUPERSEDED)
        successor = replace(
            previous,
            version=previous.version + 1,
            status=ObligationStatus.OPEN,
            statement_nl=(
                previous.statement_nl if statement_nl is None else statement_nl
            ),
            lean_statement=(
                previous.lean_statement if lean_statement is None else lean_statement
            ),
            assumptions=(
                previous.assumptions if assumptions is None else assumptions
            ),
            dependency_ids=(
                previous.dependency_ids if dependency_ids is None else dependency_ids
            ),
            rationale=(previous.rationale if rationale is None else rationale),
        )
        others = tuple(
            obligation_
            for obligation_ in self.obligations
            if obligation_.obligation_id != obligation_id
        )
        return replace(
            self, obligations=(*others, superseded, successor)
        )

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


def _detect_cycle(
    by_id: dict[str, ProofObligation],
) -> tuple[str, ...] | None:
    """Return a witness cycle path over dependency edges, or ``None``.

    Edges run ``obligation -> dependency`` (an obligation depends on its
    ``dependency_ids``). A cycle in that direction means a proof obligation
    transitively depends on itself, which the DAG invariant forbids. Uses DFS
    three-colour marking so the first back-edge found yields a concrete path.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {obligation_id: WHITE for obligation_id in by_id}
    stack: list[str] = []

    def visit(node_id: str) -> tuple[str, ...] | None:
        colour[node_id] = GREY
        stack.append(node_id)
        for dependency_id in by_id[node_id].dependency_ids:
            target = by_id.get(dependency_id)
            if target is None:
                continue
            state = colour[dependency_id]
            if state == GREY:
                # Back edge: slice the cycle out of the current path.
                start = stack.index(dependency_id)
                return tuple(stack[start:] + [dependency_id])
            if state == WHITE:
                found = visit(dependency_id)
                if found is not None:
                    return found
        stack.pop()
        colour[node_id] = BLACK
        return None

    for obligation_id in by_id:
        if colour[obligation_id] == WHITE:
            found = visit(obligation_id)
            if found is not None:
                return found
    return None


def _reverse_reachable(
    source_id: str,
    by_id: dict[str, ProofObligation],
) -> set[str]:
    """Return ids that can reach ``source_id`` following dependency edges.

    If ``B`` depends on ``A`` (edge ``B -> A``), then ``A`` is reached from
    ``B`` by walking the edge backwards. Starting at the root and walking
    backwards yields every obligation that (forwards) reaches the root.
    """
    dependents: dict[str, list[str]] = {
        obligation_id: [] for obligation_id in by_id
    }
    for obligation_id, obligation in by_id.items():
        for dependency_id in obligation.dependency_ids:
            if dependency_id in dependents:
                dependents[dependency_id].append(obligation_id)

    visited: set[str] = set()
    frontier = [source_id]
    while frontier:
        node_id = frontier.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        frontier.extend(dependents.get(node_id, []))
    return visited
