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
from agent.proof_system.workspace.action import SearchAction, SearchActionKind
from agent.proof_system.workspace.hypothesis import (
    FailureHypothesis,
    FailureKind,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
)
from agent.search.structured.frontier import (
    STALL_THRESHOLD,
    BudgetHintDefaults,
    Frontier,
    FrontierNode,
    FrontierPolicy,
    PriorityExplanation,
    soft_envelope_for_obligation,
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


def _capability_test_action(branch_id: str) -> SearchAction:
    return SearchAction(
        kind=SearchActionKind.RUN_CAPABILITY_TEST,
        target_branch_id=branch_id,
        rationale="probe capability",
    )


def _branch_with_capability_test(branch_id: str) -> ProofBranch:
    """A branch whose failure hypotheses propose a capability test.

    Such a branch is expected to run a cheap probe next (1 check, 0 model
    calls) under the cost-aware policy, ranking ahead of an equal branch that
    will implement (1 check + 1 model call).
    """
    hypothesis = FailureHypothesis(
        hypothesis_id=f"{branch_id}-h",
        kind=FailureKind.CAPABILITY_MISSING,
        confidence=0.5,
        evidence_ids=("evidence-0",),
        proposed_tests=(_capability_test_action(branch_id),),
    )
    return ProofBranch(
        branch_id=branch_id,
        obligation_id="sample",
        obligation_version=1,
        parent_branch_id=None,
        observations=(_goal_obs(0, "fp-a"),),
        status=BranchStatus.ACTIVE,
        failure_hypotheses=(hypothesis,),
    )


class FrontierPolicyTests(unittest.TestCase):
    """Phase 8.2: opt-in cost-aware frontier, legacy default unchanged."""

    def test_default_policy_is_legacy(self) -> None:
        self.assertIs(Frontier().policy, FrontierPolicy.LEGACY)

    def test_policy_values(self) -> None:
        self.assertEqual(
            {p.value for p in FrontierPolicy},
            {"legacy", "cost_aware_v1", "cost_aware_v2", "value_per_cost_v1"},
        )

    def test_legacy_default_orders_identical_to_explicit_legacy(self) -> None:
        # Two roots, same depth/attempts, differ only by branch_id: legacy key
        # is a pure branch_id tie-break regardless of how the frontier is built.
        workspace = _workspace((_branch("bbb"), _branch("aaa")))
        default_order = _drain(Frontier(), workspace)
        explicit_order = _drain(
            Frontier(policy=FrontierPolicy.LEGACY), workspace
        )
        self.assertEqual(default_order, ["aaa", "bbb"])
        self.assertEqual(default_order, explicit_order)

    def test_cost_aware_prefers_cheap_capability_probe(self) -> None:
        # Two ready branches tie on every legacy field, so legacy order is a
        # pure branch_id tie-break. The branch that will implement ("aaa",
        # dearer next action) sorts first under legacy. Cost-aware overrides the
        # tie-break to rank the cheap capability-probe branch ("zzz") first.
        cheap = _branch_with_capability_test("zzz")
        dear = _branch("aaa")
        workspace = _workspace((cheap, dear))

        legacy_order = _drain(Frontier(), workspace)
        cost_aware_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace
        )

        # Legacy tie-breaks purely on branch_id.
        self.assertEqual(legacy_order, ["aaa", "zzz"])
        # Cost-aware ranks the cheap-next-action branch first.
        self.assertEqual(cost_aware_order, ["zzz", "aaa"])

    def test_cost_aware_prefers_shallower_branch_at_equal_cost(self) -> None:
        # Root and child both implement (no proposed tests), so cost ties.
        # Cost-aware, like legacy, ranks the shallower root first.
        root = _branch("root")
        child = _branch("child", parent_branch_id="root")
        workspace = _workspace((child, root))

        cost_aware_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace
        )
        self.assertEqual(cost_aware_order, ["root", "child"])

    def test_cost_aware_ignores_subthreshold_stall_below_cost_tie(self) -> None:
        # Cost-aware collapses the stalled signal to a 0/1 penalty at the
        # threshold, so sub-threshold streak differences do not reorder at equal
        # cost. Legacy uses the raw streak as its leading key, so it ranks the
        # lower-streak branch first.
        lower_streak = _branch(
            "zzz",  # alphabetically later
            observations=(_goal_obs(0, "fp-a"), _goal_obs(1, "fp-b")),
        )
        higher_streak = _branch(
            "aaa",  # alphabetically earlier, streak 2 (still < STALL_THRESHOLD)
            observations=(_goal_obs(0, "fp-a"), _goal_obs(1, "fp-a")),
        )
        workspace = _workspace((lower_streak, higher_streak))

        legacy_order = _drain(Frontier(), workspace)
        cost_aware_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace
        )
        # Legacy: lower streak (1) beats higher streak (2).
        self.assertEqual(legacy_order, ["zzz", "aaa"])
        # Cost-aware: both sub-threshold -> equal penalty -> branch_id tie-break.
        self.assertEqual(cost_aware_order, ["aaa", "zzz"])

    def test_cost_aware_penalizes_branch_past_stall_threshold(self) -> None:
        # Once a branch exceeds the stall threshold the cost-aware penalty
        # kicks in and it loses to a progressing peer even when its branch_id
        # would otherwise win.
        progressing = _branch("zzz", observations=(_goal_obs(0, "fp-a"),))
        stalled = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-a"),
            ),
        )
        workspace = _workspace((progressing, stalled))
        cost_aware_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace
        )
        self.assertEqual(cost_aware_order, ["zzz", "aaa"])

    def test_cost_aware_branch_id_is_final_tiebreaker(self) -> None:
        # Two roots identical on every cost-aware field tie-break on branch_id.
        workspace = _workspace((_branch("zzz"), _branch("aaa")))
        cost_aware_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace
        )
        self.assertEqual(cost_aware_order, ["aaa", "zzz"])

    def test_cost_aware_keeps_readiness_as_gate(self) -> None:
        # A parent whose helper is still OPEN is not ready, so cost-aware
        # ordering never surfaces it -- readiness filters before reorder.
        helper = _branch("helper", obligation_id="sample.helper")
        parent = _branch(
            "parent",
            obligation_id="sample",
            obligation_version=2,
        )
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.OPEN, branches=(helper, parent)
        )
        cost_aware_frontier = Frontier(policy=FrontierPolicy.COST_AWARE_V1)
        cost_aware_frontier.seed(workspace)
        ready = []
        while cost_aware_frontier.has_work():
            ready.append(cost_aware_frontier.pop().branch_id)
        self.assertEqual(ready, ["helper"])

    # -- Phase 8.3: cost_aware_v2 (soft-budget overdraft deprioritisation) --

    def test_cost_aware_v2_deprioritizes_overdraft_branch(self) -> None:
        # Two roots with identical legacy / cost fields. The overdraft branch
        # has spent past its soft envelope (attempt_count > soft_checks), so V2
        # pops the still-in-envelope branch first even though its branch_id is
        # alphabetically later.
        fresh = _branch("zzz", observations=(_goal_obs(0, "fp-a"),))
        # Three distinct attempt evidence refs -> attempt_count 3, soft_checks 2
        # for a plain root (base 1 + root bonus 1).
        overdraft = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-b"),
                _goal_obs(2, "fp-c"),
            ),
        )
        workspace = _workspace((fresh, overdraft))

        v2_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V2), workspace
        )
        self.assertEqual(v2_order, ["zzz", "aaa"])

    def test_cost_aware_v2_inherits_v1_cost_ordering_when_no_overdraft(self) -> None:
        # With no overdraft on either branch, V2 falls through to the inherited
        # V1 cost dimension: the cheap capability-probe branch ranks first.
        cheap = _branch_with_capability_test("zzz")
        dear = _branch("aaa")
        workspace = _workspace((cheap, dear))

        v1_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace
        )
        v2_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V2), workspace
        )
        self.assertEqual(v1_order, ["zzz", "aaa"])
        self.assertEqual(v2_order, ["zzz", "aaa"])

    def test_cost_aware_v2_prefers_higher_unlock_value_at_equal_cost(self) -> None:
        # Two helpers at equal depth / cost / attempts: the one depended on by
        # more parents has the higher unlock value, so V2 ranks it earlier.
        # Both helpers are children of an accepted root so both are ready; the
        # multi-parent helper is listed in two parents' dependency_ids.
        from agent.proof_system.base import ProofTask

        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)
        base = workspace.obligation_graph.by_id("sample")
        assert base is not None
        hot_helper = ProofObligation(
            obligation_id="sample.hot",
            version=1,
            title="hot helper depended on by two parents",
            lean_statement="lemma hot : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        cold_helper = ProofObligation(
            obligation_id="sample.cold",
            version=1,
            title="cold helper depended on by one parent",
            lean_statement="lemma cold : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        # root v2 depends on both helpers; an extra parent depends on hot only.
        root_v2 = ProofObligation(
            obligation_id="sample",
            version=2,
            title="sample",
            lean_statement=base.lean_statement,
            dependency_ids=("sample.hot", "sample.cold"),
            status=ObligationStatus.OPEN,
        )
        extra_parent = ProofObligation(
            obligation_id="sample.extra",
            version=1,
            title="extra parent depending on hot",
            lean_statement="lemma extra : True := by trivial",
            dependency_ids=("sample.hot",),
            status=ObligationStatus.OPEN,
        )
        graph = ObligationGraph(
            obligations=(base, root_v2, extra_parent, hot_helper, cold_helper),
            root_obligation_id="sample",
        )
        branches = (
            _branch("hot", obligation_id="sample.hot"),
            _branch("cold", obligation_id="sample.cold"),
        )
        workspace = workspace.successor(obligation_graph=graph, branches=branches)

        v2_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V2), workspace
        )
        self.assertEqual(v2_order, ["hot", "cold"])

    def test_cost_aware_v2_branch_id_is_final_tiebreaker(self) -> None:
        # Two roots identical on every V2 field tie-break on branch_id.
        workspace = _workspace((_branch("zzz"), _branch("aaa")))
        v2_order = _drain(
            Frontier(policy=FrontierPolicy.COST_AWARE_V2), workspace
        )
        self.assertEqual(v2_order, ["aaa", "zzz"])

    def test_cost_aware_v2_keeps_readiness_as_gate(self) -> None:
        # A parent whose helper is still OPEN is not ready, so V2 ordering never
        # surfaces it -- readiness filters before reorder (same gate as V1).
        helper = _branch("helper", obligation_id="sample.helper")
        parent = _branch(
            "parent",
            obligation_id="sample",
            obligation_version=2,
        )
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.OPEN, branches=(helper, parent)
        )
        v2_frontier = Frontier(policy=FrontierPolicy.COST_AWARE_V2)
        v2_frontier.seed(workspace)
        ready = []
        while v2_frontier.has_work():
            ready.append(v2_frontier.pop().branch_id)
        self.assertEqual(ready, ["helper"])

    def test_cost_aware_v2_overdraft_ignores_inherited_parent_attempts(self) -> None:
        parent = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-b"),
                _goal_obs(2, "fp-c"),
            ),
        )
        repair = _branch(
            "aaa.r0",
            observations=parent.observations,
            parent_branch_id="aaa",
        )
        peer = _branch("zzz", observations=(_goal_obs(3, "fp-z"),))
        workspace = _workspace((parent, repair, peer))

        v2_frontier = Frontier(policy=FrontierPolicy.COST_AWARE_V2)
        v2_frontier.seed(workspace)
        # The repair branch has inherited evidence for prompt context, but it
        # has not spent any branch-local checks yet, so it should not be ranked
        # as overdrafted behind the peer.
        self.assertEqual(v2_frontier.pop().branch_id, "aaa.r0")

    def test_soft_envelope_uses_current_active_branch_not_old_provenance(self) -> None:
        stale = _branch(
            "old",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-a"),
            ),
            status=BranchStatus.SUPERSEDED,
            obligation_version=1,
        )
        current = _branch("current", obligation_version=2)
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.ACCEPTED,
            branches=(stale, current),
        )

        cfg = BudgetHintDefaults()
        soft_checks, soft_model_calls = soft_envelope_for_obligation(
            "sample", workspace, cfg
        )
        self.assertEqual(
            soft_checks,
            cfg.base_soft_checks + cfg.root_bonus_checks,
        )
        self.assertEqual(
            soft_model_calls,
            cfg.base_soft_model_calls + cfg.root_bonus_model_calls,
        )

    def test_legacy_and_v1_orders_unchanged_by_v2_addition(self) -> None:
        # Regression guard: adding the COST_AWARE_V2 enum value must not perturb
        # legacy or v1 ordering on identical inputs.
        progressing = _branch("zzz", observations=(_goal_obs(0, "fp-a"),))
        stalled = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-a"),
            ),
        )
        workspace = _workspace((progressing, stalled))
        # Legacy uses the raw streak as its leading key: the lower-streak
        # branch ("zzz", streak 1) pops before the stalled one ("aaa", streak 3).
        self.assertEqual(_drain(Frontier(), workspace), ["zzz", "aaa"])
        self.assertEqual(
            _drain(Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace),
            ["zzz", "aaa"],
        )


class ValuePerCostFrontierTests(unittest.TestCase):
    """Phase 8.4: opt-in value/cost mixed-score frontier ordering."""

    def _two_helper_workspace(self):
        """Workspace whose root + extra parent both depend on ``sample.hot``.

        Reuses the 8.3 multi-parent construction so ``hot`` has a higher unlock
        value (2 dependents) than ``cold`` (1 dependent), with both ready.
        """
        from agent.proof_system.base import ProofTask

        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)
        base = workspace.obligation_graph.by_id("sample")
        assert base is not None
        hot_helper = ProofObligation(
            obligation_id="sample.hot",
            version=1,
            title="hot helper depended on by two parents",
            lean_statement="lemma hot : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        cold_helper = ProofObligation(
            obligation_id="sample.cold",
            version=1,
            title="cold helper depended on by one parent",
            lean_statement="lemma cold : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        root_v2 = ProofObligation(
            obligation_id="sample",
            version=2,
            title="sample",
            lean_statement=base.lean_statement,
            dependency_ids=("sample.hot", "sample.cold"),
            status=ObligationStatus.OPEN,
        )
        extra_parent = ProofObligation(
            obligation_id="sample.extra",
            version=1,
            title="extra parent depending on hot",
            lean_statement="lemma extra : True := by trivial",
            dependency_ids=("sample.hot",),
            status=ObligationStatus.OPEN,
        )
        graph = ObligationGraph(
            obligations=(base, root_v2, extra_parent, hot_helper, cold_helper),
            root_obligation_id="sample",
        )
        branches = (
            _branch("hot", obligation_id="sample.hot"),
            _branch("cold", obligation_id="sample.cold"),
        )
        return workspace.successor(obligation_graph=graph, branches=branches)

    def test_value_per_cost_ranks_higher_unlock_value_first(self) -> None:
        # Two ready helpers at equal depth / cost / value-of-everything-else: the
        # one depended on by more parents has higher unlock value and pops first.
        workspace = self._two_helper_workspace()
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["hot", "cold"])

    def test_value_per_cost_ranks_progressing_branch_first(self) -> None:
        # Both roots, equal cost. The progressing branch changed goal
        # fingerprints on its latest attempt (progress_likelihood=1) and so pops
        # ahead of the stuck one (progress_likelihood=0). The progressing branch
        # is named later so a pure branch_id tie-break would take the other.
        progressing = _branch(
            "zzz",  # alphabetically later, so branch_id alone would NOT pick it
            observations=(_goal_obs(0, "fp-a"), _goal_obs(1, "fp-b")),
        )
        stuck = _branch(
            "aaa",
            observations=(_goal_obs(0, "fp-a"), _goal_obs(1, "fp-a")),
        )
        workspace = _workspace((progressing, stuck))
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["zzz", "aaa"])

    def test_value_per_cost_ranks_information_gain_first(self) -> None:
        # Equal cost / unlock / progress: a branch proposing a capability probe
        # (information_gain=1) ranks ahead of one that will only implement.
        informed = _branch_with_capability_test("zzz")
        plain = _branch("aaa")
        workspace = _workspace((informed, plain))
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["zzz", "aaa"])

    def test_value_per_cost_falls_back_to_branch_id_at_full_value_tie(self) -> None:
        # Every value dimension ties (no capability probe, single attempt, two
        # roots): the value-per-cost key must still deterministically fall
        # through to the branch_id tie-breaker, never reorder arbitrarily.
        workspace = _workspace((_branch("zzz"), _branch("aaa")))
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["aaa", "zzz"])

    def test_value_per_cost_cost_dimension_is_consulted(self) -> None:
        # The capability-probe branch has both lower next-action cost and higher
        # information_gain, so it wins under value-per-cost. Compared against the
        # legacy order (pure branch_id) this confirms the value/cost dimensions
        # drive the pop rather than the legacy key leaking through.
        cheap = _branch_with_capability_test("zzz")
        dear = _branch("aaa")
        workspace = _workspace((cheap, dear))
        legacy_order = _drain(Frontier(), workspace)
        value_order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(legacy_order, ["aaa", "zzz"])
        self.assertEqual(value_order, ["zzz", "aaa"])

    def test_value_per_cost_keeps_stall_as_gate(self) -> None:
        # A stalled branch with otherwise high value still loses to a fresh peer
        # because the stalled_penalty gate precedes every value dimension.
        fresh = _branch("zzz", observations=(_goal_obs(0, "fp-a"),))
        stalled = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-a"),
            ),
        )
        workspace = _workspace((fresh, stalled))
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["zzz", "aaa"])

    def test_value_per_cost_keeps_overdraft_as_gate(self) -> None:
        # A branch spent past its soft envelope loses to an in-envelope peer even
        # though both are roots with equal value, because overdraft precedes
        # value. Plain root soft_checks = base(1) + root bonus(1) = 2.
        fresh = _branch("zzz", observations=(_goal_obs(0, "fp-a"),))
        overdraft = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-b"),
                _goal_obs(2, "fp-c"),
            ),
        )
        workspace = _workspace((fresh, overdraft))
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["zzz", "aaa"])

    def test_value_per_cost_branch_id_final_tiebreaker(self) -> None:
        workspace = _workspace((_branch("zzz"), _branch("aaa")))
        order = _drain(
            Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1), workspace
        )
        self.assertEqual(order, ["aaa", "zzz"])

    def test_value_per_cost_keeps_readiness_as_gate(self) -> None:
        helper = _branch("helper", obligation_id="sample.helper")
        parent = _branch(
            "parent", obligation_id="sample", obligation_version=2
        )
        workspace = _multi_obligation_workspace(
            helper_status=ObligationStatus.OPEN, branches=(helper, parent)
        )
        frontier = Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1)
        frontier.seed(workspace)
        ready = []
        while frontier.has_work():
            ready.append(frontier.pop().branch_id)
        self.assertEqual(ready, ["helper"])

    def test_priority_explanation_recorded_on_every_pop(self) -> None:
        # Every pop, under every policy, appends a PriorityExplanation with the
        # full field set and an integer-only final_key_or_score.
        workspace = _workspace((_branch("zzz"), _branch("aaa")))
        frontier = Frontier(policy=FrontierPolicy.VALUE_PER_COST_V1)
        frontier.seed(workspace)
        first = frontier.pop()
        explanations = frontier.explanations()
        self.assertEqual(len(explanations), 1)
        expl: PriorityExplanation = explanations[0]
        self.assertEqual(expl.branch_id, first.branch_id)
        self.assertEqual(expl.policy, "value_per_cost_v1")
        self.assertEqual(expl.expected_incremental_cost, first.next_action_cost)
        self.assertEqual(expl.unlock_value, first.unlock_value)
        self.assertEqual(expl.progress_likelihood, first.progress_likelihood)
        self.assertEqual(expl.information_gain, first.information_gain)
        # final_key_or_score is a tuple of plain ints (no float, no str).
        self.assertIsInstance(expl.final_key_or_score, tuple)
        self.assertTrue(expl.final_key_or_score)
        for part in expl.final_key_or_score:
            self.assertIsInstance(part, int)
        # The serialized dict keeps the same shape.
        payload = expl.to_dict()
        self.assertEqual(payload["policy"], "value_per_cost_v1")
        self.assertIsInstance(payload["final_key_or_score"], tuple)

    def test_priority_explanation_policy_field_matches_legacy(self) -> None:
        # Legacy pops also record explanations so the trace has one uniform
        # per-pop record regardless of policy.
        workspace = _workspace((_branch("aaa"),))
        frontier = Frontier()
        frontier.seed(workspace)
        frontier.pop()
        expl = frontier.explanations()[0]
        self.assertEqual(expl.policy, "legacy")
        self.assertEqual(expl.information_gain, 0)

    def test_legacy_v1_v2_orders_unchanged_by_value_addition(self) -> None:
        # Regression guard: adding VALUE_PER_COST_V1 must not perturb legacy /
        # v1 / v2 ordering on identical inputs.
        progressing = _branch("zzz", observations=(_goal_obs(0, "fp-a"),))
        stalled = _branch(
            "aaa",
            observations=(
                _goal_obs(0, "fp-a"),
                _goal_obs(1, "fp-a"),
                _goal_obs(2, "fp-a"),
            ),
        )
        workspace = _workspace((progressing, stalled))
        self.assertEqual(_drain(Frontier(), workspace), ["zzz", "aaa"])
        self.assertEqual(
            _drain(Frontier(policy=FrontierPolicy.COST_AWARE_V1), workspace),
            ["zzz", "aaa"],
        )
        self.assertEqual(
            _drain(Frontier(policy=FrontierPolicy.COST_AWARE_V2), workspace),
            ["zzz", "aaa"],
        )


def _drain(frontier: Frontier, workspace) -> list[str]:
    frontier.seed(workspace)
    order: list[str] = []
    while frontier.has_work():
        order.append(frontier.pop().branch_id)
    return order


if __name__ == "__main__":
    unittest.main()
