from __future__ import annotations

import unittest

from agent.proof_system.workspace import (
    BranchStatus,
    ObligationGraph,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
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
    obligation_version: int = 1,
    observations: tuple[Observation, ...] = (),
    status: BranchStatus = BranchStatus.ACTIVE,
    parent_branch_id: str | None = None,
) -> ProofBranch:
    return ProofBranch(
        branch_id=branch_id,
        obligation_id=obligation_id,
        obligation_version=obligation_version,
        parent_branch_id=parent_branch_id,
        observations=observations,
        status=status,
    )


def _workspace(branches: tuple[ProofBranch, ...]):
    from agent.proof_system.base import ProofTask

    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    workspace = initialize_from_task(task)
    return workspace.successor(branches=branches)


def _multi_obligation_workspace(
    *,
    helper_status: ObligationStatus,
    branches: tuple[ProofBranch, ...],
):
    """A workspace whose root depends on a helper, with the helper's status set.

    The root obligation (``sample``) v2 depends on ``sample.helper`` v1; the
    helper's status is parameterised so readiness tests can flip it OPEN /
    ACCEPTED / BLOCKED without going through the reducer.
    """
    from agent.proof_system.base import ProofTask

    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    workspace = initialize_from_task(task)
    root_v1 = workspace.obligation_graph.by_id("sample")
    assert root_v1 is not None
    helper = ProofObligation(
        obligation_id="sample.helper",
        version=1,
        title="helper",
        lean_statement="lemma helper : True := by trivial",
        status=helper_status,
    )
    root_v2 = ProofObligation(
        obligation_id="sample",
        version=2,
        title="sample",
        lean_statement=root_v1.lean_statement,
        dependency_ids=("sample.helper",),
        status=ObligationStatus.OPEN,
    )
    graph = ObligationGraph(
        obligations=(root_v1, root_v2, helper),
        root_obligation_id="sample",
    )
    return workspace.successor(
        obligation_graph=graph,
        branches=branches,
    )


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

        # Streak counts the trailing identical attempts including the latest,
        # so three identical failures is a streak of 3.
        self.assertEqual(node.stalled_streak, 3)

    def test_single_attempt_is_streak_of_one(self) -> None:
        branch = _branch(
            "b",
            observations=(_goal_obs(0, "fp-a"),),
        )
        workspace = _workspace((branch,))

        frontier = Frontier()
        frontier.seed(workspace)
        node = frontier.pop()

        self.assertEqual(node.stalled_streak, 1)

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

        # Latest attempt moved to fp-b: streak counts only that latest batch.
        self.assertEqual(node.stalled_streak, 1)

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

        # Parent has already had its turn in this round, so only the fresh
        # child is eligible. Updating after the child begins the next round.
        self.assertEqual(frontier.pop().branch_id, "child")
        frontier.update(workspace, "child", accepted=False)
        next_round = []
        while frontier.has_work():
            next_round.append(frontier.pop().branch_id)
        self.assertIn("parent", next_round)

    def test_update_does_not_requeue_tried_branch_before_peer(self) -> None:
        first = _branch("a")
        peer = _branch("b")
        frontier = Frontier()
        frontier.seed(_workspace((first, peer)))

        self.assertEqual(frontier.pop().branch_id, "a")
        first_after = _branch("a", observations=(_goal_obs(0, "fp-a"),))
        frontier.update(_workspace((first_after, peer)), "a", accepted=False)

        self.assertEqual(frontier.pop().branch_id, "b")

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


class FrontierReadinessGateTests(unittest.TestCase):
    """Phase 7.4: an obligation is not ready until its dependencies accept."""

    def _branches(self):
        root = _branch("root", obligation_version=2)
        helper = _branch("helper", obligation_id="sample.helper")
        return root, helper

    def test_parent_not_ready_while_helper_open(self) -> None:
        root, helper = self._branches()
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.OPEN, branches=(root, helper)
        )
        frontier = Frontier()
        frontier.seed(workspace)
        # Only the helper is ready (no deps); the parent is gated out.
        ready = []
        while frontier.has_work():
            ready.append(frontier.pop().branch_id)
        self.assertEqual(ready, ["helper"])

    def test_parent_becomes_ready_when_helper_accepted(self) -> None:
        root, helper = self._branches()
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.ACCEPTED, branches=(root, helper)
        )
        frontier = Frontier()
        frontier.seed(workspace)
        ready = []
        while frontier.has_work():
            ready.append(frontier.pop().branch_id)
        # The helper is ACCEPTED so its branch has no remaining work; only the
        # parent is ready now (its dependency closed).
        self.assertEqual(ready, ["root"])

    def test_parent_never_ready_when_helper_blocked_terminates(self) -> None:
        root, helper = self._branches()
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.BLOCKED, branches=(root, helper)
        )
        frontier = Frontier()
        frontier.seed(workspace)
        # No ready branch at all: the helper is terminal-blocked, the parent's
        # dependency can never close. The frontier has no work and the loop
        # terminates (no infinite re-queue of the not-ready parent).
        self.assertFalse(frontier.has_work())

    def test_single_root_baseline_is_always_ready(self) -> None:
        # Baseline: a single root with no dependencies is ready, unchanged from
        # pre-7.4 behaviour.
        frontier = Frontier()
        frontier.seed(_workspace((_branch("root"),)))
        self.assertTrue(frontier.has_work())
        self.assertEqual(frontier.pop().branch_id, "root")


if __name__ == "__main__":
    unittest.main()
