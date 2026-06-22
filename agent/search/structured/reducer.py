"""Deterministic reducer for structured workspace transitions.

The reducer is the *only* thing that advances a :class:`ProofWorkspace` during
a structured run. It is a pure function: given the current workspace, the
:class:`SearchAction` that was executed, and a
:class:`StructuredActionResult` carrying the checker + safety outcome, it
returns the next immutable workspace. Nothing here mutates in place.

Transitions (``tmp/plan1.md`` §5/§7/§9):

* accepted + safety-accepted → the branch becomes ACCEPTED, its artifact is
  pinned, and the obligation is registered as an accepted fact with provenance;
* accepted + safety-rejected → the branch stays ACTIVE, a safety observation
  is appended so the evidence is not lost;
* check-rejected → the branch stays ACTIVE, checker observations are appended
  and the artifact is retained as provenance (a failed realization does not
  negate its mathematical strategy).

On repeated stall (same goal fingerprints across attempts) the branch is
retired to DORMANT; if a branch implementing for the first time keeps failing
on the same goals, a REPAIR_IMPLEMENTATION child branch is spawned so the
search can retry a different realization without overwriting the parent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ...proof_system.base import CheckResult
from ...proof_system.workspace import (
    BranchStatus,
    LeanArtifact,
    ProofBranch,
    SearchAction,
)
from ...proof_system.workspace.observation import (
    Observation,
    ObservationSource,
    observations_from_check_result,
)
from ..safety import SafetyVerdict
from .frontier import STALL_THRESHOLD, _stalled_streak

if TYPE_CHECKING:
    from ...proof_system.workspace import ProofWorkspace

#: Consecutive same-goal failures before a REPAIR child branch is spawned.
#: Two identical-fingerprint failures suggest the realization is stuck but the
#: strategy may still be viable, so the search forks rather than abandons.
REPAIR_THRESHOLD = 2


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
    """Return the workspace after folding ``result`` into ``action``'s branch.

    Never mutates ``workspace``; every change produces a successor (new
    ``version``). The caller's reference to the old workspace is untouched.
    """
    branch = _find_branch(workspace, result.branch_id)
    if branch is None:
        # The action targeted a branch that no longer exists (e.g. superseded
        # by a reducer transition we did not author). Drop the outcome
        # silently rather than corrupt the workspace.
        return workspace

    artifact = LeanArtifact(
        source=result.source,
        obligation_id=branch.obligation_id,
        obligation_version=branch.obligation_version,
        proof_body=result.proof_text,
    )

    if result.check_result.accepted and result.safety_verdict.accepted:
        return _accept(workspace, branch, action, artifact, result)

    return _record_failure(workspace, branch, action, artifact, result)


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
    return workspace.register_accepted_fact(
        branch.obligation_id,
        statement=result.proof_text,
        source_attempt_index=result.attempt_index,
        check_result=result.check_result,
        safety_accepted=True,
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
        # Retain the artifact as provenance: a failed realization does not
        # negate its mathematical strategy, and the trace should keep it.
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
