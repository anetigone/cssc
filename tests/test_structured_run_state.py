"""Phase 7.7: deterministic terminal workspace-status finalizer."""

from __future__ import annotations

import unittest
from dataclasses import replace

from agent.proof_system.base import ProofTask
from agent.proof_system.workspace import (
    ObligationStatus,
    ProofObligation,
    WorkspaceStatus,
    initialize_from_task,
)
from agent.search.structured.run_state import finalize_workspace_status


def _task() -> ProofTask:
    return ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")


def _seed():
    return initialize_from_task(_task())


class FinalizeWorkspaceStatusTests(unittest.TestCase):
    def test_accepted_is_accepted(self) -> None:
        self.assertEqual(
            finalize_workspace_status(_seed(), accepted=True),
            WorkspaceStatus.ACCEPTED,
        )

    def test_single_root_open_budget_exhaustion_stays_searching(self) -> None:
        # A single-root run that ran out of budget with the root still OPEN is
        # neither a partial success nor a clean failure — it must NOT be
        # mislabelled PARTIAL.
        self.assertEqual(
            finalize_workspace_status(_seed(), accepted=False),
            WorkspaceStatus.SEARCHING,
        )

    def test_verified_helper_root_open_is_partial(self) -> None:
        workspace = _seed()
        helper = ProofObligation(
            obligation_id="sample.helper1",
            version=1,
            title="h1",
            lean_statement="lemma helper1 : True := by trivial",
            status=ObligationStatus.ACCEPTED,
        )
        workspace = workspace.decompose("sample", [helper])
        # Root is OPEN, helper1 is ACCEPTED → partial result preserved.
        self.assertEqual(
            finalize_workspace_status(workspace, accepted=False),
            WorkspaceStatus.PARTIAL,
        )

    def test_all_routes_blocked_is_blocked(self) -> None:
        # Root OPEN but its only helper BLOCKED. Transitive propagation (tested
        # in test_reducer) would also flip the root; here we construct the
        # terminal state directly: root BLOCKED, helper BLOCKED, nothing
        # solvable left.
        workspace = _seed()
        helper = ProofObligation(
            obligation_id="sample.helper1",
            version=1,
            title="h1",
            lean_statement="lemma helper1 : True := by trivial",
            status=ObligationStatus.BLOCKED,
        )
        workspace = workspace.decompose("sample", [helper])
        graph = workspace.obligation_graph
        root = graph.by_id("sample")
        assert root is not None
        graph = graph.with_obligation(replace(root, status=ObligationStatus.BLOCKED))
        workspace = workspace.successor(obligation_graph=graph)
        self.assertEqual(
            finalize_workspace_status(workspace, accepted=False),
            WorkspaceStatus.BLOCKED,
        )

    def test_open_obligation_with_verified_helper_is_partial_not_blocked(self) -> None:
        # Solvable work remains AND a helper is verified → PARTIAL (the open
        # route means it is not a dead-end, the helper means it is not empty).
        workspace = _seed()
        helper1 = ProofObligation(
            obligation_id="sample.helper1",
            version=1,
            title="h1",
            lean_statement="lemma helper1 : True := by trivial",
            status=ObligationStatus.ACCEPTED,
        )
        helper2 = ProofObligation(
            obligation_id="sample.helper2",
            version=1,
            title="h2",
            lean_statement="lemma helper2 : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        workspace = workspace.decompose("sample", [helper1, helper2])
        self.assertEqual(
            finalize_workspace_status(workspace, accepted=False),
            WorkspaceStatus.PARTIAL,
        )


if __name__ == "__main__":
    unittest.main()
