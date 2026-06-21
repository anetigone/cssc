"""Top-level structured-mode workspace and its factory."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Sequence

from ..base import CheckResult, DiagnosticCategory
from .branch import BranchStatus, ProofBranch, proof_branch_from_dict
from .graph import ObligationGraph, obligation_graph_from_dict
from .obligation import ObligationStatus, ProofObligation
from .spec import (
    FormalSpecification,
    VerifiedFact,
    WorkspaceStatus,
    formal_specification_from_dict,
    verified_fact_from_dict,
)

if TYPE_CHECKING:
    from ...tasks.types import ProofTask


@dataclass(frozen=True)
class ProofWorkspaceReport:
    """Deterministic result of validating authoritative workspace state."""

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


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
    branches: tuple[ProofBranch, ...] = ()

    root_obligation_ids: tuple[str, ...] = ()
    status: WorkspaceStatus = WorkspaceStatus.INITIALIZING

    def validate(self) -> ProofWorkspaceReport:
        """Check obligation, branch-tree, and cross-object references."""
        errors = list(self.obligation_graph.validate().errors)
        obligation_versions = {
            (obligation.obligation_id, obligation.version): obligation
            for obligation in self.obligation_graph.obligations
        }

        branches_by_id: dict[str, ProofBranch] = {}
        for branch in self.branches:
            if branch.branch_id in branches_by_id:
                errors.append(f"duplicate proof branch id {branch.branch_id!r}")
            else:
                branches_by_id[branch.branch_id] = branch
            errors.extend(branch.validate().errors)

            obligation = obligation_versions.get(
                (branch.obligation_id, branch.obligation_version)
            )
            if obligation is None:
                errors.append(
                    f"branch {branch.branch_id!r} references missing obligation "
                    f"{branch.obligation_id!r} v{branch.obligation_version}"
                )
            elif (
                obligation.status == ObligationStatus.SUPERSEDED
                and branch.status != BranchStatus.SUPERSEDED
            ):
                errors.append(
                    f"branch {branch.branch_id!r} remains {branch.status.value} "
                    f"on superseded obligation {branch.obligation_id!r} "
                    f"v{branch.obligation_version}"
                )

        for branch in self.branches:
            if (
                branch.parent_branch_id is not None
                and branch.parent_branch_id not in branches_by_id
            ):
                errors.append(
                    f"branch {branch.branch_id!r} references missing parent branch "
                    f"{branch.parent_branch_id!r}"
                )

        cycle = _detect_branch_cycle(branches_by_id)
        if cycle is not None:
            errors.append(f"branch parent cycle detected: {' -> '.join(cycle)}")

        return ProofWorkspaceReport(ok=not errors, errors=tuple(errors))

    def successor(
        self,
        *,
        obligation_graph: ObligationGraph | None = None,
        accepted_facts: tuple[VerifiedFact, ...] | None = None,
        branches: tuple[ProofBranch, ...] | None = None,
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
                obligation_graph
                if obligation_graph is not None
                else self.obligation_graph
            ),
            accepted_facts=(
                accepted_facts if accepted_facts is not None else self.accepted_facts
            ),
            branches=branches if branches is not None else self.branches,
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
            "branches": [branch.to_dict() for branch in self.branches],
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
        branches=tuple(
            proof_branch_from_dict(item) for item in data.get("branches", ())
        ),
        root_obligation_ids=tuple(data.get("root_obligation_ids", ())),
        status=WorkspaceStatus(
            data.get("status", WorkspaceStatus.INITIALIZING.value)
        ),
    )


def _detect_branch_cycle(
    branches_by_id: dict[str, ProofBranch],
) -> tuple[str, ...] | None:
    """Return a parent-link cycle witness, if one exists."""
    for start_id in branches_by_id:
        path: list[str] = []
        positions: dict[str, int] = {}
        current_id: str | None = start_id
        while current_id is not None and current_id in branches_by_id:
            if current_id in positions:
                start = positions[current_id]
                return tuple(path[start:] + [current_id])
            positions[current_id] = len(path)
            path.append(current_id)
            current_id = branches_by_id[current_id].parent_branch_id
    return None


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
