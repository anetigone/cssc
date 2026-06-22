from __future__ import annotations

import unittest

from agent.proof_system.workspace import (
    BranchStatus,
    ProofBranch,
    initialize_from_task,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
)
from agent.search.structured.frontier import (
    STALL_THRESHOLD,
    Frontier,
    FrontierNode,
)


def _goal_obs(
    attempt: int, fingerprint: str, *, declaration: str | None = None
) -> Observation:
    return Observation(
        observation_id=f"attempt:{attempt}:goal:0",
        source=ObservationSource.CHECKER,
        category="unsolved_goal",
        message="goal text",
        declaration_id=declaration,
        goal_fingerprint=fingerprint,
        raw_evidence_ref=f"attempt:{attempt}",
    )


def _branch(
    branch_id: str,
    *,
    obligation_id: str = "sample",
    observations: tuple[Observation, ...] = (),
    status: BranchStatus = BranchStatus.ACTIVE,
    parent_branch_id: str | None = None,
) -> ProofBranch:
    return ProofBranch(
        branch_id=branch_id,
        obligation_id=obligation_id,
        obligation_version=1,
        parent_branch_id=parent_branch_id,
        observations=observations,
        status=status,
    )


def _workspace(branches: tuple[ProofBranch, ...]):
    from agent.proof_system.base import ProofTask

    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    workspace = initialize_from_task(task)
    return workspace.successor(branches=branches)


class FrontierSeedAndPopTests(unittest.TestCase):
    def test_seed_loads_active_branches_only(self) -> None:
        active = _branch("b1")
        dormant = _branch("b2", status=BranchStatus.DORMANT)
        accepted = _branch("b3", status=BranchStatus.ACCEPTED)
        workspace = _workspace((active, dormant, accepted))

        frontier = Frontier()
        frontier.seed(workspace)

        self.assertTrue(frontier.has_work())
        node = frontier.pop()
        self.assertEqual(node.branch_id, "b1")
        self.assertFalse(frontier.has_work())

    def test_empty_workspace_has_no_work(self) -> None:
        workspace = _workspace(())
        frontier = Frontier()
        frontier.seed(workspace)
        self.assertFalse(frontier.has_work())

    def test_pop_raises_when_empty(self) -> None:
        frontier = Frontier()
        frontier.seed(_workspace(()))
        with self.assertRaises(StopIteration):
            frontier.pop()


class FrontierOrderingTests(unittest.TestCase):
    def test_fewer_attempts_pops_first(self) -> None:
        newer = _branch("newer")
        older = _branch(
            "older",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
            ),
        )
        workspace = _workspace((newer, older))

        frontier = Frontier()
        frontier.seed(workspace)
        popped = [frontier.pop().branch_id for _ in range(2)]

        self.assertEqual(popped, ["newer", "older"])

    def test_stalled_branch_loses_priority(self) -> None:
        progressing = _branch(
            "progressing",
            observations=(
                _goal_obs(0, "fp-x"),
                _goal_obs(1, "fp-y"),
            ),
        )
        stalled = _branch(
            "stalled",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
            ),
        )
        workspace = _workspace((progressing, stalled))

        frontier = Frontier()
        frontier.seed(workspace)
        popped = [frontier.pop().branch_id for _ in range(2)]

        self.assertEqual(popped, ["progressing", "stalled"])

    def test_shallower_depth_pops_first(self) -> None:
        root = _branch("root")
        child = _branch("child", parent_branch_id="root")
        workspace = _workspace((child, root))

        frontier = Frontier()
        frontier.seed(workspace)
        popped = [frontier.pop().branch_id for _ in range(2)]

        self.assertEqual(popped, ["root", "child"])

    def test_selection_is_deterministic_across_seeds(self) -> None:
        a = _branch("zzz")
        b = _branch("aaa")
        c = _branch("mmm")
        workspace = _workspace((a, b, c))

        order_one = []
        frontier = Frontier()
        frontier.seed(workspace)
        while frontier.has_work():
            order_one.append(frontier.pop().branch_id)

        order_two = []
        frontier = Frontier()
        frontier.seed(workspace)
        while frontier.has_work():
            order_two.append(frontier.pop().branch_id)

        self.assertEqual(order_one, order_two)
        self.assertEqual(order_one, ["aaa", "mmm", "zzz"])


class FrontierStallDetectionTests(unittest.TestCase):
    def test_stalled_streak_counts_repeated_fingerprints(self) -> None:
        branch = _branch(
            "b",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-a"),
            ),
        )
        workspace = _workspace((branch,))

        frontier = Frontier()
        frontier.seed(workspace)
        node = frontier.pop()

        self.assertEqual(node.stalled_streak, 2)

    def test_progressing_streak_resets_on_change(self) -> None:
        branch = _branch(
            "b",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-b"),
            ),
        )
        workspace = _workspace((branch,))

        frontier = Frontier()
        frontier.seed(workspace)
        node = frontier.pop()

        self.assertEqual(node.stalled_streak, 0)

    def test_stall_threshold_constant(self) -> None:
        # Lock the threshold so the reducer's retirement rule stays in sync.
        self.assertEqual(STALL_THRESHOLD, 3)


class FrontierUpdateTests(unittest.TestCase):
    def test_update_drops_retired_branch(self) -> None:
        branch = _branch("b")
        workspace = _workspace((branch,))

        frontier = Frontier()
        frontier.seed(workspace)
        node = frontier.pop()
        self.assertEqual(node.branch_id, "b")

        # Reducer retired the branch to DORMANT after this attempt.
        retired = _branch("b", status=BranchStatus.DORMANT)
        workspace = _workspace((retired,))
        frontier.update(workspace, "b", accepted=False)

        self.assertFalse(frontier.has_work())

    def test_update_requeues_branch_that_stayed_active(self) -> None:
        branch = _branch("b")
        workspace = _workspace((branch,))

        frontier = Frontier()
        frontier.seed(workspace)
        frontier.pop()

        still_active = _branch(
            "b", observations=(_goal_obs(0, "fp-a"),)
        )
        workspace = _workspace((still_active,))
        frontier.update(workspace, "b", accepted=False)

        self.assertTrue(frontier.has_work())
        node = frontier.pop()
        self.assertEqual(node.branch_id, "b")

    def test_update_picks_up_new_child_branch(self) -> None:
        parent = _branch("parent")
        workspace = _workspace((parent,))

        frontier = Frontier()
        frontier.seed(workspace)
        frontier.pop()

        # Reducer spawned a REPAIR child.
        parent_after = _branch(
            "parent",
            observations=(_goal_obs(0, "fp-a"), _goal_obs(1, "fp-a")),
        )
        child = _branch(
            "child",
            parent_branch_id="parent",
            observations=(_goal_obs(0, "fp-a"),),
        )
        workspace = _workspace((parent_after, child))
        frontier.update(workspace, "parent", accepted=False)

        popped_ids = []
        while frontier.has_work():
            popped_ids.append(frontier.pop().branch_id)

        # Parent has two stalled attempts; the fresh child wins.
        self.assertEqual(popped_ids[0], "child")
        self.assertIn("parent", popped_ids)

    def test_popped_branch_id_carries_obligation(self) -> None:
        node = FrontierNode(
            branch_id="b1",
            obligation_id="root",
            depth_from_root=0,
            attempt_count=0,
            last_goal_fingerprints=("fp-a",),
            stalled_streak=0,
        )
        self.assertEqual(node.obligation_id, "root")


if __name__ == "__main__":
    unittest.main()
