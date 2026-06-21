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
from typing import TYPE_CHECKING, Any, Sequence

from .base import CheckResult, DiagnosticCategory

if TYPE_CHECKING:
    # Avoid an eager import of the tasks package at module load; only the type
    # checker needs ``ProofTask`` for the ``initialize_from_task`` annotation.
    from ..tasks.types import ProofTask


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
    """

    obligation_id: str
    obligation_version: int
    statement: str
    source_attempt_index: int
    checker_category: str
    safety_accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "statement": self.statement,
            "source_attempt_index": self.source_attempt_index,
            "checker_category": self.checker_category,
            "safety_accepted": self.safety_accepted,
        }


def verified_fact_from_dict(data: dict[str, Any]) -> VerifiedFact:
    return VerifiedFact(
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        statement=data["statement"],
        source_attempt_index=int(data["source_attempt_index"]),
        checker_category=data["checker_category"],
        safety_accepted=bool(data["safety_accepted"]),
    )


@dataclass(frozen=True)
class ProofWorkspace:
    """The authoritative structured-mode search state.

    Field shape follows the Phase 3 design note (``tmp/plan1.md`` §3). A
    workspace is immutable: every mutation (decomposition, accepted fact, new
    obligation version) returns a successor workspace with a bumped ``version``
    and ``parent_version`` pointing back. The minimal loop never constructs one.
    """

    workspace_id: str
    version: int = 1
    parent_version: int | None = None

    specification: FormalSpecification = field(default_factory=FormalSpecification)
    obligation_graph: ObligationGraph = field(default_factory=ObligationGraph)
    accepted_facts: tuple[VerifiedFact, ...] = ()

    root_obligation_ids: tuple[str, ...] = ()
    status: WorkspaceStatus = WorkspaceStatus.INITIALIZING

    def successor(
        self,
        *,
        obligation_graph: ObligationGraph | None = None,
        accepted_facts: tuple[VerifiedFact, ...] | None = None,
        root_obligation_ids: tuple[str, ...] | None = None,
        status: WorkspaceStatus | None = None,
    ) -> ProofWorkspace:
        """Return the next workspace version with the supplied fields changed.

        Centralizes the version bump so every mutation records the parent it
        descended from, keeping the structured search history replayable.
        """
        return replace(
            self,
            version=self.version + 1,
            parent_version=self.version,
            obligation_graph=(
                obligation_graph if obligation_graph is not None else self.obligation_graph
            ),
            accepted_facts=(
                accepted_facts if accepted_facts is not None else self.accepted_facts
            ),
            root_obligation_ids=(
                root_obligation_ids
                if root_obligation_ids is not None
                else self.root_obligation_ids
            ),
            status=status if status is not None else self.status,
        )

    def decompose(
        self,
        obligation_id: str,
        children: Sequence[ProofObligation],
    ) -> ProofWorkspace:
        """Split an obligation into auxiliary child obligations.

        Each child is inserted with its own declared dependencies, then the
        parent receives a new version that depends on those children. This is
        the proof-dependency direction: the parent cannot be accepted until
        all child obligations are available.

        Phase 3 only wires the graph mutation; deciding *when* to decompose is
        the frontier policy's job (Phase 6).
        """
        graph = self.obligation_graph
        if graph.by_id(obligation_id) is None:
            raise KeyError(f"unknown obligation {obligation_id!r}")
        existing_ids = {
            obligation.obligation_id for obligation in graph.obligations
        }
        child_ids: list[str] = []
        for child in children:
            if child.obligation_id == obligation_id:
                raise ValueError("an obligation cannot be its own decomposition child")
            if child.obligation_id in existing_ids or child.obligation_id in child_ids:
                raise ValueError(
                    f"duplicate active child obligation {child.obligation_id!r}"
                )
            graph = graph.with_obligation(child)
            child_ids.append(child.obligation_id)
        parent = graph.by_id(obligation_id)
        assert parent is not None
        dependencies = tuple(dict.fromkeys((*parent.dependency_ids, *child_ids)))
        graph = graph.new_version(obligation_id, dependency_ids=dependencies)
        return self.successor(obligation_graph=graph)

    def register_accepted_fact(
        self,
        obligation_id: str,
        *,
        statement: str,
        source_attempt_index: int,
        check_result: CheckResult,
        safety_accepted: bool,
    ) -> ProofWorkspace:
        """Mark an obligation ACCEPTED and record a provenance-carrying fact.

        The fact is pinned to the obligation's current version and may only be
        registered from a checker-accepted, safety-accepted attempt.
        """
        graph = self.obligation_graph
        obligation = graph.by_id(obligation_id)
        if obligation is None:
            raise KeyError(f"unknown obligation {obligation_id!r}")
        if obligation.status == ObligationStatus.SUPERSEDED:
            raise ValueError(
                f"cannot register a fact against superseded obligation "
                f"{obligation_id!r}"
            )
        if not check_result.accepted:
            raise ValueError("cannot register a fact from a rejected checker result")
        if check_result.category != DiagnosticCategory.PROOF_ACCEPTED:
            raise ValueError(
                "accepted checker result must use the proof_accepted category"
            )
        if not safety_accepted:
            raise ValueError("cannot register a fact rejected by the safety reviewer")
        if source_attempt_index < 0:
            raise ValueError("source_attempt_index must be non-negative")
        accepted = replace(obligation, status=ObligationStatus.ACCEPTED)
        new_graph = graph.with_obligation(accepted)
        fact = VerifiedFact(
            obligation_id=obligation.obligation_id,
            obligation_version=obligation.version,
            statement=statement,
            source_attempt_index=source_attempt_index,
            checker_category=check_result.category.value,
            safety_accepted=True,
        )
        accepted_facts = (*self.accepted_facts, fact)
        return self.successor(
            obligation_graph=new_graph,
            accepted_facts=accepted_facts,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "specification": self.specification.to_dict(),
            "obligation_graph": self.obligation_graph.to_dict(),
            "accepted_facts": [fact.to_dict() for fact in self.accepted_facts],
            "root_obligation_ids": list(self.root_obligation_ids),
            "status": self.status.value,
        }


def workspace_from_dict(data: dict[str, Any]) -> ProofWorkspace:
    return ProofWorkspace(
        workspace_id=data["workspace_id"],
        version=int(data.get("version", 1)),
        parent_version=data.get("parent_version"),
        specification=formal_specification_from_dict(
            data.get("specification", {}) or {}
        ),
        obligation_graph=obligation_graph_from_dict(
            data.get("obligation_graph", {}) or {}
        ),
        accepted_facts=tuple(
            verified_fact_from_dict(item) for item in data.get("accepted_facts", ())
        ),
        root_obligation_ids=tuple(data.get("root_obligation_ids", ())),
        status=WorkspaceStatus(
            data.get("status", WorkspaceStatus.INITIALIZING.value)
        ),
    )


def initialize_from_task(task: ProofTask) -> ProofWorkspace:
    """Seed a single-root workspace from a checker-ready :class:`ProofTask`.

    The structured run begins with exactly one root obligation derived from the
    task's verifier-facing source. The root ``lean_statement`` is the full
    ``source_template`` (the hole marker stays in place; a later phase replaces
    it with a proved artifact), and ``statement_nl`` is taken from the task's
    natural-language provenance in metadata when present. Phase 3 does not
    decompose automatically — decomposition is an explicit later action.
    """
    metadata = dict(task.metadata)
    statement_nl = str(metadata.get("natural_language_problem") or "").strip()

    root = ProofObligation(
        obligation_id=task.task_id,
        version=1,
        title=task.task_id,
        statement_nl=statement_nl,
        lean_statement=task.source_template,
        status=ObligationStatus.OPEN,
    )
    graph = ObligationGraph(
        obligations=(root,),
        root_obligation_id=task.task_id,
    )
    specification = FormalSpecification(
        statement_nl=statement_nl,
        lean_statement=task.source_template,
        source_task_id=task.task_id,
    )
    return ProofWorkspace(
        workspace_id=task.task_id,
        version=1,
        parent_version=None,
        specification=specification,
        obligation_graph=graph,
        accepted_facts=(),
        root_obligation_ids=(task.task_id,),
        status=WorkspaceStatus.SEARCHING,
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
