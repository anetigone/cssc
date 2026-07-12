from __future__ import annotations

import unittest
from dataclasses import replace

from agent.proof_system.base import ProofTask
from agent.proof_system.workspace import (
    DEFAULT_ALLOWED_MUTATIONS,
    BranchStatus,
    ProofBranch,
    SearchAction,
    SearchActionKind,
    initialize_from_task,
)
from agent.search.structured.action_frontier import (
    ActionFrontier,
    ActionFrontierPolicy,
    CostEstimate,
    Estimate,
    ProposalCache,
    ProposalCacheLimits,
    proposal_cache_from_dict,
)
from agent.search.structured.proposal import (
    CapabilityTestPayload,
    DecomposePayload,
    ImplementPayload,
    StructuredActionProposal,
)
from agent.search.budget import BudgetSnapshot
from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)
from agent.search.structured.budget_snapshot import (
    ActionBudgetLimits,
    build_unified_budget_snapshot,
)
from agent.search.structured.controller.action_runtime_selection import (
    select_admissible_action,
)


def _workspace(*branches: ProofBranch):
    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    return initialize_from_task(task).successor(branches=branches)


def _proposal(branch_id: str, kind: SearchActionKind) -> StructuredActionProposal:
    payload = {
        SearchActionKind.IMPLEMENT: ImplementPayload("trivial"),
        SearchActionKind.DECOMPOSE: DecomposePayload(()),
        SearchActionKind.RUN_CAPABILITY_TEST: CapabilityTestPayload("True", "#check True"),
    }[kind]
    return StructuredActionProposal(
        action=SearchAction(
            kind=kind,
            target_branch_id=branch_id,
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[kind],
            rationale="test action",
        ),
        payload=payload,
    )


class ProposalCacheTests(unittest.TestCase):
    def test_same_branch_actions_can_compete(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, reasons = ProposalCache().add(
            workspace,
            (_proposal("b1", SearchActionKind.IMPLEMENT), _proposal("b1", SearchActionKind.RUN_CAPABILITY_TEST), _proposal("b1", SearchActionKind.DECOMPOSE)),
            proposal_source="model", proposal_batch_id="batch-1", proposal_model_tier="cheap",
        )
        self.assertEqual(reasons, ())
        self.assertEqual({node.proposal.action.kind for node in cache.entries}, {
            SearchActionKind.IMPLEMENT, SearchActionKind.RUN_CAPABILITY_TEST, SearchActionKind.DECOMPOSE,
        })
        self.assertTrue(all(node.proposal_batch_id == "batch-1" for node in cache.entries))

    def test_stale_branch_version_is_invalidated_not_rebound(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, _ = ProposalCache().add(workspace, (_proposal("b1", SearchActionKind.IMPLEMENT),), proposal_source="model")
        stale_workspace = workspace.successor(branches=(replace(workspace.branches[0], obligation_version=2),))
        frontier = ActionFrontier()
        refreshed = frontier.refresh(stale_workspace, cache)
        self.assertFalse(frontier.has_work())
        self.assertEqual(refreshed.entries, ())

    def test_pending_limits_are_enforced(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, reasons = ProposalCache(limits=ProposalCacheLimits(per_branch_pending=1, global_pending=1)).add(
            workspace,
            (_proposal("b1", SearchActionKind.IMPLEMENT), _proposal("b1", SearchActionKind.RUN_CAPABILITY_TEST)),
            proposal_source="model",
        )
        self.assertEqual(len(cache.entries), 1)
        self.assertEqual(reasons, ("global_pending_limit",))

    def test_cache_round_trip_preserves_pinned_nodes(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, _ = ProposalCache().add(
            workspace, (_proposal("b1", SearchActionKind.IMPLEMENT),),
            proposal_source="model", proposal_batch_id="batch-1", proposal_model_tier="cheap",
        )
        restored = proposal_cache_from_dict(cache.to_dict())
        self.assertEqual(restored, cache)


class ActionFrontierTests(unittest.TestCase):
    def test_constrained_selection_skips_rejected_higher_ranked_action(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, _ = ProposalCache().add(
            workspace,
            (_proposal("b1", SearchActionKind.DECOMPOSE), _proposal("b1", SearchActionKind.IMPLEMENT)),
            proposal_source="model",
        )
        by_kind = {node.proposal.action.kind: node.node_id for node in cache.entries}
        cache = cache.with_estimates({
            by_kind[SearchActionKind.DECOMPOSE]: CostEstimate(
                checks=Estimate(0), api_cost_usd=Estimate(10)
            ),
            by_kind[SearchActionKind.IMPLEMENT]: CostEstimate(
                checks=Estimate(0), api_cost_usd=Estimate(1)
            ),
        })
        frontier = ActionFrontier(policy=ActionFrontierPolicy.COST_AWARE_V1)
        frontier.refresh(workspace, cache)
        ledger = CostLedger((CostLedgerEvent(
            event_id="charge", kind=CostLedgerEventKind.CHARGE,
            scope=CostScope.PROPOSAL_GENERATION, status="completed",
            api_cost_usd=CostMeasurement.observed(0),
        ),))
        snapshot = build_unified_budget_snapshot(
            BudgetSnapshot(0, 0, 0, 5, 5), ledger,
            limits=ActionBudgetLimits(max_api_cost_usd=5),
        )

        selection = select_admissible_action(frontier, snapshot)

        self.assertEqual(
            selection.node.proposal.action.kind, SearchActionKind.IMPLEMENT
        )
        self.assertFalse(selection.choice_set[0]["budget_admission"]["allowed"])
        self.assertTrue(selection.choice_set[1]["budget_admission"]["allowed"])

        unconstrained = select_admissible_action(
            frontier, snapshot, enforce_remaining_budget=False
        )
        self.assertEqual(
            unconstrained.node.proposal.action.kind, SearchActionKind.DECOMPOSE
        )
        self.assertTrue(unconstrained.admission.allowed)
        self.assertIn(
            "remaining_budget_policy_disabled",
            unconstrained.admission.not_compared_dimensions,
        )

    def test_cost_policy_prefers_free_structural_action(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, _ = ProposalCache().add(
            workspace,
            (_proposal("b1", SearchActionKind.IMPLEMENT), _proposal("b1", SearchActionKind.DECOMPOSE)),
            proposal_source="model",
        )
        frontier = ActionFrontier(policy=ActionFrontierPolicy.COST_AWARE_V1)
        frontier.refresh(workspace, cache)
        selected = frontier.pop()
        self.assertEqual(selected.proposal.action.kind, SearchActionKind.DECOMPOSE)
        self.assertEqual(selected.priority_explanation.expected_incremental_cost, 0)

    def test_replay_order_is_deterministic(self) -> None:
        workspace = _workspace(ProofBranch("a", "sample", 1), ProofBranch("b", "sample", 1))
        proposals = (_proposal("b", SearchActionKind.IMPLEMENT), _proposal("a", SearchActionKind.IMPLEMENT))
        cache, _ = ProposalCache().add(workspace, proposals, proposal_source="model")
        def replay() -> list[str]:
            frontier = ActionFrontier()
            frontier.refresh(workspace, cache)
            return [frontier.pop().node_id for _ in range(2)]
        self.assertEqual(replay(), replay())

    def test_frozen_estimates_can_change_action_order(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, _ = ProposalCache().add(
            workspace,
            (_proposal("b1", SearchActionKind.IMPLEMENT), _proposal("b1", SearchActionKind.DECOMPOSE)),
            proposal_source="model",
        )
        by_kind = {node.proposal.action.kind: node.node_id for node in cache.entries}
        historical = cache.with_estimates({
            by_kind[SearchActionKind.IMPLEMENT]: CostEstimate(checks=Estimate(1), source="history", sample_count=3),
            by_kind[SearchActionKind.DECOMPOSE]: CostEstimate(checks=Estimate(5), source="history", sample_count=3),
        })
        frontier = ActionFrontier(policy=ActionFrontierPolicy.COST_AWARE_V1)
        frontier.refresh(workspace, historical)
        self.assertEqual(frontier.pop().proposal.action.kind, SearchActionKind.IMPLEMENT)

    def test_unknown_check_cost_does_not_become_infinite_cost(self) -> None:
        workspace = _workspace(ProofBranch("b1", "sample", 1))
        cache, _ = ProposalCache().add(
            workspace,
            (_proposal("b1", SearchActionKind.IMPLEMENT), _proposal("b1", SearchActionKind.DECOMPOSE)),
            proposal_source="model",
        )
        by_kind = {node.proposal.action.kind: node.node_id for node in cache.entries}
        cache = cache.with_estimates({
            by_kind[SearchActionKind.IMPLEMENT]: CostEstimate(
                checks=Estimate(1), source="history", sample_count=3
            ),
            by_kind[SearchActionKind.DECOMPOSE]: CostEstimate(
                checks=None, source="history", sample_count=3
            ),
        })
        frontier = ActionFrontier(policy=ActionFrontierPolicy.COST_AWARE_V1)
        frontier.refresh(workspace, cache)
        # With an incomplete dimension, the round falls back to non-cost keys;
        # DECOMPOSE remains a real competitor instead of receiving +infinity.
        selected = frontier.pop()
        self.assertEqual(selected.proposal.action.kind, SearchActionKind.DECOMPOSE)
        self.assertIsNone(selected.priority_explanation.expected_incremental_cost)


if __name__ == "__main__":
    unittest.main()
