"""Obligation DAG and deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .obligation import ObligationStatus, ProofObligation, obligation_from_dict


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
    single root obligation whose dependency closure contains every auxiliary
    obligation; decomposition may add further proof dependencies.
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
        """Return a graph with one exact obligation version inserted/replaced.

        Versions with the same id but a different version number are retained
        for provenance. Version lifecycle transitions therefore replace only
        the exact ``(obligation_id, version)`` slot.
        """
        updated: list[ProofObligation] = []
        replaced = False
        for current in self.obligations:
            if (
                current.obligation_id == obligation.obligation_id
                and current.version == obligation.version
            ):
                if not replaced:
                    updated.append(obligation)
                    replaced = True
                continue
            updated.append(current)
        if not replaced:
            updated.append(obligation)
        return replace(self, obligations=tuple(updated))

    def validate(self) -> ObligationGraphReport:
        """Check DAG invariants without raising.

        Verifies:

        * the root obligation exists and is not superseded;
        * every ``dependency_id`` refers to an obligation in the graph;
        * no active obligation depends on a superseded version;
        * the dependency edges form a DAG (no cycles);
        * every non-root active obligation is reachable from the root through
          the root's proof-dependency closure.

        Returns a report; ``ok`` is ``True`` iff ``errors`` is empty.
        """
        errors: list[str] = []

        ids = {obligation.obligation_id for obligation in self.obligations}
        seen_versions: set[tuple[str, int]] = set()
        active_counts: dict[str, int] = {}
        for obligation in self.obligations:
            key = (obligation.obligation_id, obligation.version)
            if obligation.version < 1:
                errors.append(
                    f"obligation {obligation.obligation_id!r} has invalid "
                    f"version {obligation.version}"
                )
            if key in seen_versions:
                errors.append(
                    f"duplicate obligation version {obligation.obligation_id!r} "
                    f"v{obligation.version}"
                )
            seen_versions.add(key)
            if obligation.status != ObligationStatus.SUPERSEDED:
                active_counts[obligation.obligation_id] = (
                    active_counts.get(obligation.obligation_id, 0) + 1
                )
        for obligation_id, count in active_counts.items():
            if count > 1:
                errors.append(
                    f"obligation {obligation_id!r} has {count} active versions"
                )
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

        # Every active obligation belongs to the root's proof dependency
        # closure. Edges point from an obligation to facts it depends on, so
        # decomposition makes the parent/root depend on its helper children.
        if root is not None:
            reachable = _dependency_reachable(root.obligation_id, active_by_id)
            for obligation in active_by_id.values():
                if obligation.obligation_id == root.obligation_id:
                    continue
                if obligation.obligation_id not in reachable:
                    errors.append(
                        f"obligation {obligation.obligation_id!r} cannot be "
                        f"reached from root obligation {root.obligation_id!r}"
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
        graph = self.with_obligation(superseded)
        return replace(graph, obligations=(*graph.obligations, successor))

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


def _dependency_reachable(
    source_id: str,
    by_id: dict[str, ProofObligation],
) -> set[str]:
    """Return the proof dependencies reachable from ``source_id``."""
    visited: set[str] = set()
    frontier = [source_id]
    while frontier:
        node_id = frontier.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        obligation = by_id.get(node_id)
        if obligation is not None:
            frontier.extend(
                dependency_id
                for dependency_id in obligation.dependency_ids
                if dependency_id in by_id
            )
    return visited
