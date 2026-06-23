"""Structured workspace context projection.

Phase 6 drove the structured loop but handed the proof generator only a
"minimal-style" context: the branch's obligation id + two statement strings,
a flat list of accepted facts, and a ``previous_attempt`` carrying just the
proof body plus raw observations. The real workspace state — argument steps,
the goal↔step alignment, the current obligation's dependency closure, the
competing failure hypotheses, and sibling strategies on the same obligation —
never reached the prompt.

This module is a pure derivation over a :class:`ProofWorkspace` plus a
``branch_id``: :func:`build_context_projection` returns a frozen
:class:`StructuredContextProjection` whose ``to_dict`` shape is stable and
renderable. It adds no new dependencies and never mutates the workspace; the
minimal loop does not import it (it is structured-only, like the rest of this
sub-package, and deliberately absent from ``__init__.__all__``).

The projection crosses the structured→prompt boundary as a plain dict: the
shared :mod:`agent.agents.proof` renderer duck-types it via
``Mapping``/``Sequence`` checks, exactly like the existing ``branch_obligation``
/``verified_facts`` keys, so ``proof.py`` never imports this package and the
minimal path pays nothing.

Deliberate non-decisions:

* Observation/branch counts are capped (see :data:`MAX_PROJECTED_OBSERVATIONS`
  and :data:`MAX_SIBLING_BRANCHES`) to bound prompt growth; everything else is
  bounded by the workspace DAGs themselves.
* Version-staleness is enforced for dependency facts (a fact from obligation
  v1 is not reused against v2), mirroring the :class:`VerifiedFact` provenance
  invariant.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...proof_system.workspace import ProofWorkspace

if TYPE_CHECKING:
    pass


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
    open dependency rather than a reusable conclusion.
    """

    obligation_id: str
    obligation_version: int
    statement: str
    has_accepted_fact: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "statement": self.statement,
            "has_accepted_fact": self.has_accepted_fact,
        }


def dependency_fact_from_dict(data: dict[str, Any]) -> DependencyFact:
    return DependencyFact(
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        statement=data.get("statement", ""),
        has_accepted_fact=bool(data.get("has_accepted_fact", False)),
    )


@dataclass(frozen=True)
class AcceptedFactSlot:
    """One reusable accepted fact."""

    obligation_id: str
    statement: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "statement": self.statement,
        }


def accepted_fact_slot_from_dict(data: dict[str, Any]) -> AcceptedFactSlot:
    return AcceptedFactSlot(
        obligation_id=data["obligation_id"],
        statement=data.get("statement", ""),
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


@dataclass(frozen=True)
class StructuredContextProjection:
    """The structured workspace projected onto prompt-renderable slots.

    ``branch_id`` is ``None`` only when :func:`build_context_projection` could
    not resolve the requested branch; the root obligation is still derived when
    the graph has one (best-effort, never raises), and every per-branch section
    is empty.
    """

    workspace_id: str
    workspace_version: int
    branch_id: str | None
    root: ObligationSlot | None
    current_obligation: ObligationSlot | None
    dependency_facts: tuple[DependencyFact, ...]
    accepted_facts: tuple[AcceptedFactSlot, ...]
    argument_steps: tuple[ArgumentStepSlot, ...]
    lean_artifact_proof_body: str | None
    observations: tuple[ObservationSlot, ...]
    failure_hypotheses: tuple[FailureHypothesisSlot, ...]
    sibling_branches: tuple[SiblingBranchSlot, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_version": self.workspace_version,
            "branch_id": self.branch_id,
            "root": self.root.to_dict() if self.root is not None else None,
            "current_obligation": (
                self.current_obligation.to_dict()
                if self.current_obligation is not None
                else None
            ),
            "dependency_facts": [item.to_dict() for item in self.dependency_facts],
            "accepted_facts": [item.to_dict() for item in self.accepted_facts],
            "argument_steps": [item.to_dict() for item in self.argument_steps],
            "lean_artifact_proof_body": self.lean_artifact_proof_body,
            "observations": [item.to_dict() for item in self.observations],
            "failure_hypotheses": [
                item.to_dict() for item in self.failure_hypotheses
            ],
            "sibling_branches": [
                item.to_dict() for item in self.sibling_branches
            ],
        }


def context_projection_from_dict(
    data: dict[str, Any],
) -> StructuredContextProjection:
    root = data.get("root")
    current = data.get("current_obligation")
    return StructuredContextProjection(
        workspace_id=data["workspace_id"],
        workspace_version=int(data["workspace_version"]),
        branch_id=data.get("branch_id"),
        root=obligation_slot_from_dict(root) if isinstance(root, dict) else None,
        current_obligation=(
            obligation_slot_from_dict(current) if isinstance(current, dict) else None
        ),
        dependency_facts=tuple(
            dependency_fact_from_dict(item)
            for item in data.get("dependency_facts", ())
            if isinstance(item, dict)
        ),
        accepted_facts=tuple(
            accepted_fact_slot_from_dict(item)
            for item in data.get("accepted_facts", ())
            if isinstance(item, dict)
        ),
        argument_steps=tuple(
            argument_step_slot_from_dict(item)
            for item in data.get("argument_steps", ())
            if isinstance(item, dict)
        ),
        lean_artifact_proof_body=data.get("lean_artifact_proof_body"),
        observations=tuple(
            observation_slot_from_dict(item)
            for item in data.get("observations", ())
            if isinstance(item, dict)
        ),
        failure_hypotheses=tuple(
            failure_hypothesis_slot_from_dict(item)
            for item in data.get("failure_hypotheses", ())
            if isinstance(item, dict)
        ),
        sibling_branches=tuple(
            sibling_branch_slot_from_dict(item)
            for item in data.get("sibling_branches", ())
            if isinstance(item, dict)
        ),
    )


def build_context_projection(
    workspace: ProofWorkspace, branch_id: str
) -> StructuredContextProjection:
    """Derive the prompt projection for ``branch_id`` within ``workspace``.

    Pure and never raises: an unresolvable ``branch_id`` yields a projection
    with ``branch_id=None`` and empty per-branch sections while the root
    obligation is still surfaced when the graph has one.
    """
    graph = workspace.obligation_graph
    root_obs = graph.root()

    branch = next(
        (b for b in workspace.branches if b.branch_id == branch_id), None
    )

    current_obs = None
    if branch is not None:
        current_obs = graph.by_id(branch.obligation_id)

    root_id = root_obs.obligation_id if root_obs is not None else None

    def _slot(obs: Any, is_root: bool) -> ObligationSlot:
        return ObligationSlot(
            obligation_id=obs.obligation_id,
            version=obs.version,
            title=obs.title,
            statement_nl=obs.statement_nl,
            lean_statement=obs.lean_statement,
            is_root=is_root,
        )

    root_slot = _slot(root_obs, True) if root_obs is not None else None
    current_slot = (
        _slot(current_obs, current_obs.obligation_id == root_id)
        if current_obs is not None
        else None
    )

    dependency_facts: tuple[DependencyFact, ...] = ()
    accepted_facts: tuple[AcceptedFactSlot, ...] = ()
    argument_steps: tuple[ArgumentStepSlot, ...] = ()
    lean_artifact_proof_body: str | None = None
    observations: tuple[ObservationSlot, ...] = ()
    failure_hypotheses: tuple[FailureHypothesisSlot, ...] = ()
    sibling_branches: tuple[SiblingBranchSlot, ...] = ()

    if branch is not None:
        dependency_facts = _dependency_facts(workspace, current_obs)
        argument_steps = _argument_steps(branch)
        lean_artifact_proof_body = (
            branch.lean_artifact.proof_body
            if branch.lean_artifact is not None
            else None
        )
        observations = _observations(branch)
        failure_hypotheses = _failure_hypotheses(branch)
        sibling_branches = _sibling_branches(workspace, branch)

    accepted_facts = tuple(
        AcceptedFactSlot(
            obligation_id=fact.obligation_id, statement=fact.statement
        )
        for fact in workspace.accepted_facts
    )

    return StructuredContextProjection(
        workspace_id=workspace.workspace_id,
        workspace_version=workspace.version,
        branch_id=branch.branch_id if branch is not None else None,
        root=root_slot,
        current_obligation=current_slot,
        dependency_facts=dependency_facts,
        accepted_facts=accepted_facts,
        argument_steps=argument_steps,
        lean_artifact_proof_body=lean_artifact_proof_body,
        observations=observations,
        failure_hypotheses=failure_hypotheses,
        sibling_branches=sibling_branches,
    )


def _dependency_facts(
    workspace: ProofWorkspace, current_obs: Any
) -> tuple[DependencyFact, ...]:
    """Resolve the dependency closure of ``current_obs`` to fact slots.

    Edges run root → helper (an obligation depends on its ``dependency_ids``),
    so the transitive walk collects every helper the current obligation needs.
    A helper matches an accepted fact only when both the id and the obligation
    version agree, which rejects facts proven against a superseded version.
    """
    if current_obs is None:
        return ()
    graph = workspace.obligation_graph
    closure: list[str] = []
    seen: set[str] = set()
    queue: deque[str] = deque(current_obs.dependency_ids)
    while queue:
        dep_id = queue.popleft()
        if dep_id in seen:
            continue
        seen.add(dep_id)
        dep = graph.by_id(dep_id)
        if dep is None:
            # Missing dependency: record the id as an open helper with no fact.
            closure.append(dep_id)
            continue
        closure.append(dep_id)
        queue.extend(dep.dependency_ids)

    facts_by_version = {
        (fact.obligation_id, fact.obligation_version): fact
        for fact in workspace.accepted_facts
    }
    slots: list[DependencyFact] = []
    for dep_id in closure:
        dep = graph.by_id(dep_id)
        if dep is None:
            slots.append(
                DependencyFact(
                    obligation_id=dep_id,
                    obligation_version=0,
                    statement="",
                    has_accepted_fact=False,
                )
            )
            continue
        matched = facts_by_version.get((dep.obligation_id, dep.version))
        slots.append(
            DependencyFact(
                obligation_id=dep.obligation_id,
                obligation_version=dep.version,
                statement=(
                    matched.statement if matched is not None else dep.lean_statement
                ),
                has_accepted_fact=matched is not None,
            )
        )
    return tuple(slots)


def _argument_steps(branch: Any) -> tuple[ArgumentStepSlot, ...]:
    """Map argument steps to slots, attaching the first matching alignment."""
    alignment_by_step = {
        link.argument_step_id: link for link in branch.alignment
    }
    slots: list[ArgumentStepSlot] = []
    for step in branch.argument.steps:
        link = alignment_by_step.get(step.step_id)
        slots.append(
            ArgumentStepSlot(
                step_id=step.step_id,
                claim=step.claim,
                justification=step.justification,
                depends_on=step.depends_on,
                alignment_relation=(
                    link.relation.value if link is not None else None
                ),
                aligned_declaration=(
                    link.lean_declaration_id
                    if link is not None
                    else None
                ),
            )
        )
    return tuple(slots)


def _observations(branch: Any) -> tuple[ObservationSlot, ...]:
    """Deduplicate observations by ``(goal_fingerprint, message)`` and cap.

    Observations accumulate across attempts, so duplicates (the same goal
    fingerprint reported every retry) collapse to one entry; the tail — the
    most recent evidence — is what survives the cap.
    """
    deduped: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for obs in branch.observations:
        key = (obs.goal_fingerprint or "", obs.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(obs)
    if len(deduped) > MAX_PROJECTED_OBSERVATIONS:
        deduped = deduped[-MAX_PROJECTED_OBSERVATIONS:]
    return tuple(
        ObservationSlot(
            observation_id=obs.observation_id,
            source=obs.source.value,
            category=obs.category,
            message=obs.message,
            goal_fingerprint=obs.goal_fingerprint,
            raw_evidence_ref=obs.raw_evidence_ref,
        )
        for obs in deduped
    )


def _failure_hypotheses(branch: Any) -> tuple[FailureHypothesisSlot, ...]:
    return tuple(
        FailureHypothesisSlot(
            hypothesis_id=hyp.hypothesis_id,
            kind=hyp.kind.value,
            confidence=hyp.confidence,
            evidence_ids=hyp.evidence_ids,
            affected_step_ids=hyp.affected_step_ids,
        )
        for hyp in branch.failure_hypotheses
    )


def _sibling_branches(
    workspace: ProofWorkspace, branch: Any
) -> tuple[SiblingBranchSlot, ...]:
    """Other strategies on the same obligation (any version), status only."""
    siblings = [
        b
        for b in workspace.branches
        if b.obligation_id == branch.obligation_id and b.branch_id != branch.branch_id
    ]
    slots = [
        SiblingBranchSlot(
            branch_id=b.branch_id,
            status=b.status.value,
            has_artifact=b.lean_artifact is not None,
            observation_count=len(b.observations),
        )
        for b in siblings
    ]
    return tuple(slots[:MAX_SIBLING_BRANCHES])
