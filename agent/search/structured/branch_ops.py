"""Pure branch/workspace operations used by the structured controller.

These helpers are deliberately stateless: they take a :class:`ProofWorkspace`
and return a successor workspace (or a branch/view), never mutating the input.
Keeping them out of :mod:`.controller` shortens the controller file and makes
the branch lifecycle easier to test in isolation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...proof_system.base import ProofTask
from ...proof_system.workspace import (
    BranchStatus,
    ProofBranch,
    ProofWorkspace,
    SearchAction,
    SearchActionKind,
)


def root_branch_id(task: ProofTask) -> str:
    return f"{task.task_id}:root"


def branch_by_id(workspace: ProofWorkspace, branch_id: str) -> ProofBranch | None:
    for branch in workspace.branches:
        if branch.branch_id == branch_id:
            return branch
    return None


def action_rationale(kind: SearchActionKind, branch: ProofBranch) -> str:
    if kind == SearchActionKind.IMPLEMENT:
        return f"initial implementation for branch {branch.branch_id}"
    return f"repair implementation for branch {branch.branch_id}"


def block_branch(workspace: ProofWorkspace, branch_id: str) -> ProofWorkspace:
    branches = tuple(
        replace(branch, status=BranchStatus.BLOCKED)
        if branch.branch_id == branch_id
        else branch
        for branch in workspace.branches
    )
    return workspace.successor(branches=branches)


def expand_candidate_branches(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    count: int,
    batch_index: int,
) -> tuple[ProofWorkspace, tuple[ProofBranch, ...]]:
    """Materialize one branch per candidate without overwriting alternatives."""
    if count <= 1:
        return workspace, (branch,)
    alternatives = [branch]
    existing_ids = {item.branch_id for item in workspace.branches}
    for candidate_index in range(1, count):
        candidate_id = f"{branch.branch_id}.c{batch_index}.{candidate_index}"
        suffix = 0
        while candidate_id in existing_ids:
            suffix += 1
            candidate_id = (
                f"{branch.branch_id}.c{batch_index}.{candidate_index}.{suffix}"
            )
        existing_ids.add(candidate_id)
        alternatives.append(
            replace(
                branch,
                branch_id=candidate_id,
                parent_branch_id=branch.branch_id,
                lean_artifact=None,
                last_action=None,
                status=BranchStatus.ACTIVE,
            )
        )
    return (
        workspace.successor(branches=(*workspace.branches, *alternatives[1:])),
        tuple(alternatives),
    )


def edit_with_structured_metadata(
    edit: Any, action: SearchAction, branch: ProofBranch
) -> Any:
    metadata = dict(edit.metadata)
    metadata["structured_action_kind"] = action.kind.value
    metadata["structured_branch_id"] = branch.branch_id
    return replace(edit, metadata=metadata)
