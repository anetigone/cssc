"""Decomposition reducer transition for structured search."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from agent.proof_system.workspace import (
    BranchStatus,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    SearchAction,
    SearchActionKind,
)
from ..proposal import DecomposeChildSpec

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofWorkspace


def apply_decompose(
    workspace: ProofWorkspace,
    action: SearchAction,
    *,
    children: Sequence[DecomposeChildSpec],
    parent_branch_id: str,
) -> ProofWorkspace:
    """Fold a ``DECOMPOSE`` action: split an obligation into helper children.

    A separate entry point from the checker-result reducer because
    decomposition is a structural move: it carries no ``CheckResult``, no safety
    verdict, no artifact, and it spawns several new branches.
    """
    if action.kind is not SearchActionKind.DECOMPOSE:
        return workspace
    branch = _find_branch(workspace, parent_branch_id)
    if branch is None:
        return workspace
    graph = workspace.obligation_graph
    current = graph.by_id(branch.obligation_id)
    if current is None or current.version != branch.obligation_version:
        return workspace
    if not children:
        return workspace

    child_ids = [child.child_id for child in children]
    child_obligations: list[ProofObligation] = []
    for child in children:
        narrowed_deps = tuple(
            dep_id for dep_id in child.dependency_ids if dep_id in child_ids
        )
        child_obligations.append(
            ProofObligation(
                obligation_id=child.child_id,
                version=1,
                title=child.child_id,
                lean_statement=child.statement,
                dependency_ids=narrowed_deps,
                status=ObligationStatus.OPEN,
            )
        )

    parent_version_before = current.version
    workspace = workspace.decompose(branch.obligation_id, child_obligations)

    new_parent = workspace.obligation_graph.by_id(branch.obligation_id)
    assert new_parent is not None and new_parent.version > parent_version_before

    retired_branches = tuple(
        replace(existing, status=BranchStatus.SUPERSEDED)
        if (
            existing.obligation_id == branch.obligation_id
            and existing.obligation_version == parent_version_before
        )
        else existing
        for existing in workspace.branches
    )

    parent_branch = replace(
        branch,
        branch_id=_next_parent_branch_id(branch.branch_id, workspace.branches),
        obligation_version=new_parent.version,
        lean_artifact=None,
        observations=(),
        status=BranchStatus.ACTIVE,
    )
    child_branches = tuple(
        ProofBranch(
            branch_id=f"{branch.branch_id}.d.{child_id}",
            obligation_id=child_id,
            obligation_version=1,
            parent_branch_id=branch.branch_id,
            status=BranchStatus.ACTIVE,
        )
        for child_id in child_ids
    )
    return workspace.successor(
        branches=(*retired_branches, parent_branch, *child_branches)
    )


def _next_parent_branch_id(
    parent_branch_id: str, branches: tuple[ProofBranch, ...]
) -> str:
    """Deterministic fresh branch_id for a post-decompose parent branch."""
    prefix = f"{parent_branch_id}.p"
    count = sum(1 for b in branches if b.branch_id.startswith(prefix))
    return f"{prefix}{count}"


def _find_branch(
    workspace: ProofWorkspace, branch_id: str
) -> ProofBranch | None:
    for branch in workspace.branches:
        if branch.branch_id == branch_id:
            return branch
    return None
