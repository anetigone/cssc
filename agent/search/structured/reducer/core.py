"""Pure reducer for structured workspace transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agent.proof_system.base import CheckResult, DiagnosticCategory
from agent.proof_system.workspace import (
    ArtifactKind,
    BranchStatus,
    LeanArtifact,
    ObligationGraph,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    SearchAction,
    SearchActionKind,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
    observations_from_check_result,
)
from agent.search.safety import SafetyVerdict
from ..frontier import STALL_THRESHOLD, _stalled_streak
from ..proposal import DecomposeChildSpec
from .decompose import apply_decompose
from .structural import (
    apply_argument,
    apply_change_representation,
    apply_failure_hypotheses,
)

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofWorkspace

REPAIR_THRESHOLD = 2

_DECLARATION_ID_RE = re.compile(
    r"^[ \t]*(?:private[ \t]+)?(?:noncomputable[ \t]+)?"
    r"(?:theorem|lemma|def)[ \t]+([^\s:({]+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class StructuredActionResult:
    """Everything the reducer needs to fold one executed action's outcome."""

    branch_id: str
    check_result: CheckResult
    safety_verdict: SafetyVerdict
    proof_text: str
    source: str
    attempt_index: int


def apply(
    workspace: ProofWorkspace,
    action: SearchAction,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Return the workspace after folding ``result`` into ``action``'s branch."""
    branch = _find_branch(workspace, result.branch_id)
    if branch is None:
        return workspace

    if action.kind is SearchActionKind.RUN_CAPABILITY_TEST:
        return _apply_capability_audit(workspace, branch, action, result)

    is_root = branch.obligation_id in workspace.root_obligation_ids
    artifact = LeanArtifact(
        source=result.source,
        obligation_id=branch.obligation_id,
        obligation_version=branch.obligation_version,
        proof_body=result.proof_text,
        declaration_id=None if is_root else _declaration_id(result.source),
        kind=ArtifactKind.PROOF_BODY if is_root else ArtifactKind.DECLARATION,
    )

    if result.check_result.accepted and result.safety_verdict.accepted:
        return _accept(workspace, branch, action, artifact, result)

    return _record_failure(workspace, branch, action, artifact, result)


def _declaration_id(source: str) -> str | None:
    """Best-effort Lean declaration name from a standalone helper artifact."""
    match = _DECLARATION_ID_RE.search(source)
    if match is None:
        return None
    return match.group(1)


def _accept(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    action: SearchAction,
    artifact: LeanArtifact,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Mark the branch ACCEPTED and register the obligation as a verified fact."""
    accepted_branch = replace(
        branch,
        lean_artifact=artifact,
        last_action=action,
        status=BranchStatus.ACCEPTED,
    )
    new_branches = _replace_branch(workspace.branches, accepted_branch)
    workspace = workspace.successor(branches=new_branches)
    workspace = workspace.register_accepted_fact(
        branch.obligation_id,
        statement=artifact.source,
        source_attempt_index=result.attempt_index,
        check_result=result.check_result,
        safety_accepted=True,
        declaration_id=artifact.declaration_id,
        artifact_source=artifact.source,
    )
    return _reactivate_dormant(
        workspace, trigger_obligation_id=branch.obligation_id
    )


def _record_failure(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    action: SearchAction,
    artifact: LeanArtifact,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Append evidence to an ACTIVE branch, retiring or forking it if stalled."""
    new_observations = _observations_for(result)
    updated_branch = replace(
        branch,
        lean_artifact=artifact,
        last_action=action,
        observations=(*branch.observations, *new_observations),
    )

    stalled = _stalled_streak(updated_branch)
    if stalled >= STALL_THRESHOLD:
        updated_branch = replace(updated_branch, status=BranchStatus.DORMANT)

    new_branches = _replace_branch(workspace.branches, updated_branch)
    if _should_spawn_repair_child(updated_branch, action, new_branches):
        new_branches = (*new_branches, _make_repair_child(updated_branch, new_branches))

    return workspace.successor(branches=new_branches)


_CAPABILITY_MISSING_CATEGORIES: frozenset[DiagnosticCategory] = frozenset(
    {
        DiagnosticCategory.UNKNOWN_IDENTIFIER,
        DiagnosticCategory.INVALID_REFERENCE,
        DiagnosticCategory.TOOL_UNAVAILABLE,
    }
)


def _capability_missing(check_result: CheckResult) -> bool:
    """True iff the checker reports an unavailable environment resource."""
    return check_result.category in _CAPABILITY_MISSING_CATEGORIES


def _apply_capability_audit(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    action: SearchAction,
    result: StructuredActionResult,
) -> ProofWorkspace:
    """Fold a capability-audit outcome into the branch."""
    observation = _capability_observation(result)
    updated_branch = replace(
        branch,
        last_action=action,
        observations=(*branch.observations, observation),
    )

    if not _capability_missing(result.check_result):
        new_branches = _replace_branch(workspace.branches, updated_branch)
        workspace = workspace.successor(branches=new_branches)
        return _reactivate_dormant(
            workspace, trigger_obligation_id=branch.obligation_id
        )

    blocked_branch = replace(updated_branch, status=BranchStatus.BLOCKED)
    new_branches = _replace_branch(workspace.branches, blocked_branch)
    workspace = workspace.successor(branches=new_branches)
    return _block_obligation(workspace, branch.obligation_id)


def _capability_observation(result: StructuredActionResult) -> Observation:
    evidence_ref = f"capability:{result.attempt_index}"
    check = result.check_result
    feedback = check.parsed_feedback
    message = (
        feedback.message
        if feedback is not None and feedback.message
        else (check.raw_output.strip()[:160] if check.raw_output else "")
    )
    prefix = "capability available" if check.accepted else "capability probe failed"
    if message:
        message = f"{prefix}: {message}"
    else:
        message = prefix
    return Observation(
        observation_id=f"{evidence_ref}:capability",
        source=ObservationSource.CAPABILITY_AUDIT,
        category=check.category.value,
        message=message,
        raw_evidence_ref=evidence_ref,
    )


def _block_obligation(
    workspace: ProofWorkspace, obligation_id: str
) -> ProofWorkspace:
    """Block an obligation and all active dependents."""
    graph = workspace.obligation_graph
    obligation = graph.by_id(obligation_id)
    if obligation is None:
        return workspace
    dependents: dict[str, list[str]] = {}
    for active in graph.active():
        for dependency_id in active.dependency_ids:
            dependents.setdefault(dependency_id, []).append(
                active.obligation_id
            )
    closure: set[str] = set()
    frontier = [obligation_id]
    while frontier:
        current = frontier.pop()
        if current in closure:
            continue
        closure.add(current)
        frontier.extend(dependents.get(current, ()))
    new_graph = graph
    blocked_pins: set[tuple[str, int]] = set()
    for blocked_id in closure:
        current = new_graph.by_id(blocked_id)
        if current is None:
            continue
        if current.status not in (
            ObligationStatus.OPEN,
            ObligationStatus.IN_PROGRESS,
        ):
            continue
        blocked_pins.add((current.obligation_id, current.version))
        new_graph = new_graph.with_obligation(
            replace(current, status=ObligationStatus.BLOCKED)
        )
    new_branches = tuple(
        replace(branch, status=BranchStatus.BLOCKED)
        if (
            (branch.obligation_id, branch.obligation_version) in blocked_pins
            and branch.status in (BranchStatus.ACTIVE, BranchStatus.DORMANT)
        )
        else branch
        for branch in workspace.branches
    )
    return workspace.successor(obligation_graph=new_graph, branches=new_branches)


def _reactivate_dormant(
    workspace: ProofWorkspace, *, trigger_obligation_id: str
) -> ProofWorkspace:
    """Revive DORMANT branches that may use new evidence."""
    graph = workspace.obligation_graph
    dependent_ids: set[str] = {trigger_obligation_id}
    for active in graph.active():
        reachable = _dependency_closure(active.obligation_id, graph)
        if trigger_obligation_id in reachable:
            dependent_ids.add(active.obligation_id)
    revived = False
    new_branches = list(workspace.branches)
    for index, branch in enumerate(new_branches):
        current = graph.by_id(branch.obligation_id)
        if (
            branch.status == BranchStatus.DORMANT
            and branch.obligation_id in dependent_ids
            and current is not None
            and current.version == branch.obligation_version
            and current.status
            in (ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS)
        ):
            new_branches[index] = replace(branch, status=BranchStatus.ACTIVE)
            revived = True
    if not revived:
        return workspace
    return workspace.successor(branches=tuple(new_branches))


def _dependency_closure(
    obligation_id: str, graph: ObligationGraph
) -> set[str]:
    """Obligation ids reachable from ``obligation_id`` along dependency edges."""
    visited: set[str] = set()
    frontier = [obligation_id]
    while frontier:
        current_id = frontier.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        current = graph.by_id(current_id)
        if current is None:
            continue
        frontier.extend(current.dependency_ids)
    return visited


def _observations_for(result: StructuredActionResult) -> tuple[Observation, ...]:
    """Neutral observations for a failure, plus a safety note if relevant."""
    observations = list(observations_from_check_result(
        result.check_result, result.attempt_index
    ))
    if result.check_result.accepted and not result.safety_verdict.accepted:
        observations.append(_safety_observation(result))
    return tuple(observations)


def _safety_observation(result: StructuredActionResult) -> Observation:
    evidence_ref = f"attempt:{result.attempt_index}"
    return Observation(
        observation_id=f"{evidence_ref}:safety",
        source=ObservationSource.CHECKER,
        category="safety_rejected",
        message="; ".join(result.safety_verdict.reasons) or "safety review rejected",
        raw_evidence_ref=evidence_ref,
    )


def _should_spawn_repair_child(
    branch: ProofBranch,
    action: SearchAction,
    branches: tuple[ProofBranch, ...],
) -> bool:
    """Spawn a REPAIR child when a root strategy branch stalls repeatedly.

    Forking is bounded: only a branch that is itself a root strategy attempt
    (no ``parent_branch_id``) and has not already spawned a repair child may
    fork. This keeps the branch tree shallow — a stalled repair child retires
    to DORMANT via the stall threshold rather than spawning nested siblings —
    while still giving the search one fresh realization attempt per stalled
    strategy. The action kind (IMPLEMENT vs REPAIR_IMPLEMENTATION) is
    irrelevant: what matters is that the realization keeps failing on the same
    goals.
    """
    del action  # fork rule is stall-driven, not action-kind-driven
    if branch.parent_branch_id is not None:
        return False
    if branch.status == BranchStatus.ACCEPTED:
        return False
    if _stalled_streak(branch) < REPAIR_THRESHOLD:
        return False
    prefix = f"{branch.branch_id}.r"
    if any(sibling.branch_id.startswith(prefix) for sibling in branches):
        return False
    return True


def _make_repair_child(
    parent: ProofBranch, branches: tuple[ProofBranch, ...]
) -> ProofBranch:
    """Derive a REPAIR child branch from a stalled parent.

    Inherits the argument, alignment, and accumulated observations (so the
    child sees the failure evidence that motivated the fork) but starts
    without a Lean artifact — the next IMPLEMENT will supply a fresh
    realization. A new ``branch_id`` (``<parent>.r<n>``) guarantees the parent
    is never overwritten; ``n`` counts existing repair siblings so forks are
    deterministic.
    """
    prefix = f"{parent.branch_id}.r"
    child_index = sum(
        1 for branch in branches if branch.branch_id.startswith(prefix)
    )
    return ProofBranch(
        branch_id=f"{parent.branch_id}.r{child_index}",
        obligation_id=parent.obligation_id,
        obligation_version=parent.obligation_version,
        parent_branch_id=parent.branch_id,
        argument=parent.argument,
        alignment=parent.alignment,
        observations=parent.observations,
        lean_artifact=None,
        status=BranchStatus.ACTIVE,
    )


def _replace_branch(
    branches: tuple[ProofBranch, ...], updated: ProofBranch
) -> tuple[ProofBranch, ...]:
    """Return a copy of ``branches`` with ``updated`` substituted in place."""
    replaced = tuple(
        updated if branch.branch_id == updated.branch_id else branch
        for branch in branches
    )
    if not any(branch.branch_id == updated.branch_id for branch in replaced):
        raise KeyError(
            f"branch {updated.branch_id!r} not present in workspace branches"
        )
    return replaced


def _find_branch(
    workspace: ProofWorkspace, branch_id: str
) -> ProofBranch | None:
    for branch in workspace.branches:
        if branch.branch_id == branch_id:
            return branch
    return None
