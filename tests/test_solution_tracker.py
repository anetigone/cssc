from __future__ import annotations

import unittest
from dataclasses import replace

from agent.proof_system.base import ProofTask
from agent.proof_system.workspace import (
    BranchStatus,
    ProofBranch,
    initialize_from_task,
)
from agent.proof_system.workspace.artifact import LeanArtifact
from agent.proof_system.workspace.obligation import ObligationStatus
from agent.search.structured.solution_tracker import (
    has_complete_solution,
    select_solution,
)


def _artifact(obligation_id: str, version: int = 1) -> LeanArtifact:
    return LeanArtifact(
        source=f"theorem {obligation_id} : True := by trivial",
        obligation_id=obligation_id,
        obligation_version=version,
        proof_body="trivial",
    )


def _workspace_with_accepted_root(
    *,
    branch_id: str = "root-branch",
    artifact: LeanArtifact | None = None,
    branch_obligation_version: int = 1,
    branch_status: BranchStatus = BranchStatus.ACCEPTED,
):
    """A single-root workspace whose root obligation is ACCEPTED."""
    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    workspace = initialize_from_task(task)
    root = workspace.obligation_graph.root()
    assert root is not None
    accepted_obligation = replace(root, status=ObligationStatus.ACCEPTED)
    graph = workspace.obligation_graph.with_obligation(accepted_obligation)
    branch = ProofBranch(
        branch_id=branch_id,
        obligation_id=root.obligation_id,
        obligation_version=branch_obligation_version,
        lean_artifact=artifact,
        status=branch_status,
    )
    return workspace.successor(obligation_graph=graph, branches=(branch,))


class HasCompleteSolutionTests(unittest.TestCase):
    def test_open_root_is_not_complete(self) -> None:
        workspace = initialize_from_task(
            ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        )
        self.assertFalse(has_complete_solution(workspace))

    def test_accepted_branch_without_artifact_is_not_complete(self) -> None:
        workspace = _workspace_with_accepted_root(artifact=None)
        self.assertFalse(has_complete_solution(workspace))

    def test_accepted_branch_with_artifact_is_complete(self) -> None:
        workspace = _workspace_with_accepted_root(artifact=_artifact("sample"))
        self.assertTrue(has_complete_solution(workspace))

    def test_non_accepted_branch_is_not_complete(self) -> None:
        workspace = _workspace_with_accepted_root(
            artifact=_artifact("sample"),
            branch_status=BranchStatus.ACTIVE,
        )
        self.assertFalse(has_complete_solution(workspace))

    def test_stale_version_branch_is_not_complete(self) -> None:
        # Branch pins version 1 but the obligation is on version 2.
        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)
        root = workspace.obligation_graph.root()
        assert root is not None
        graph = workspace.obligation_graph.new_version(
            "sample",
            lean_statement="theorem sample : True := by\n  trivial\n",
        )
        current = graph.by_id("sample")
        assert current is not None
        accepted = replace(current, status=ObligationStatus.ACCEPTED)
        graph = graph.with_obligation(accepted)
        stale_branch = ProofBranch(
            branch_id="stale",
            obligation_id="sample",
            obligation_version=1,
            lean_artifact=_artifact("sample", version=1),
            status=BranchStatus.ACCEPTED,
        )
        workspace = workspace.successor(obligation_graph=graph, branches=(stale_branch,))
        self.assertFalse(has_complete_solution(workspace))

    def test_invalid_workspace_is_not_complete(self) -> None:
        # A branch pinning a missing obligation version makes the workspace
        # invalid, which the tracker must reject rather than paper over.
        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)
        bad_branch = ProofBranch(
            branch_id="bad",
            obligation_id="sample",
            obligation_version=99,
            lean_artifact=_artifact("sample", version=99),
            status=BranchStatus.ACCEPTED,
        )
        workspace = workspace.successor(branches=(bad_branch,))
        self.assertFalse(has_complete_solution(workspace))


class SelectSolutionTests(unittest.TestCase):
    def test_selects_the_accepted_branch_with_artifact(self) -> None:
        artifact = _artifact("sample")
        workspace = _workspace_with_accepted_root(
            branch_id="root-branch", artifact=artifact
        )
        solution = select_solution(workspace)
        self.assertEqual(len(solution), 1)
        self.assertEqual(solution[0].branch_id, "root-branch")
        self.assertEqual(solution[0].lean_artifact, artifact)

    def test_picks_smallest_branch_id_when_multiple_accepted(self) -> None:
        artifact = _artifact("sample")
        # Two accepted branches on the same obligation: OR choice preserved.
        first = ProofBranch(
            branch_id="zzz",
            obligation_id="sample",
            obligation_version=1,
            lean_artifact=artifact,
            status=BranchStatus.ACCEPTED,
        )
        second = ProofBranch(
            branch_id="aaa",
            obligation_id="sample",
            obligation_version=1,
            lean_artifact=artifact,
            status=BranchStatus.ACCEPTED,
        )
        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)
        root = workspace.obligation_graph.root()
        assert root is not None
        accepted = replace(root, status=ObligationStatus.ACCEPTED)
        graph = workspace.obligation_graph.with_obligation(accepted)
        workspace = workspace.successor(
            obligation_graph=graph, branches=(first, second)
        )

        solution = select_solution(workspace)
        self.assertEqual(solution[0].branch_id, "aaa")

    def test_empty_solution_when_not_complete(self) -> None:
        workspace = initialize_from_task(
            ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        )
        self.assertEqual(select_solution(workspace), ())


if __name__ == "__main__":
    unittest.main()
