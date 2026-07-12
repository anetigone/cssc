"""Structured soft-budget hints and borrowing.

Covers :mod:`agent.search.structured.budget_hints`: the per-obligation soft
envelope (root / unlock-value / capability / stalled / accepted-neighbour
components), the realised ``borrowed_*`` join from the cost summary, the
no-mutation / no-reserve contract, and the single-root invariant that an
overdraft never flips a run to PARTIAL/BLOCKED.
"""

from __future__ import annotations

import unittest

from agent.proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ProofTask,
)
from agent.proof_system.workspace import (
    BranchStatus,
    ObligationGraph,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    WorkspaceStatus,
    initialize_from_task,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
)
from agent.proof_system.workspace.spec import VerifiedFact
from agent.search.budget import BudgetSnapshot
from agent.search.controller.types import AttemptRecord
from agent.search.structured.budget_hints import (
    ObligationBudgetHint,
    build_obligation_budget_hints,
    join_borrowed_costs,
)
from agent.search.structured.costing import build_cost_summary
from agent.search.structured.frontier import BudgetHintDefaults


def _task() -> ProofTask:
    return ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")


def _seed():
    return initialize_from_task(_task())


def _branch(
    branch_id: str,
    obligation_id: str,
    *,
    observations: tuple[Observation, ...] = (),
    status: BranchStatus = BranchStatus.ACTIVE,
) -> ProofBranch:
    return ProofBranch(
        branch_id=branch_id,
        obligation_id=obligation_id,
        obligation_version=1,
        observations=observations,
        status=status,
    )


def _goal_obs(attempt: int, fingerprint: str) -> Observation:
    return Observation(
        observation_id=f"attempt:{attempt}:goal:0",
        source=ObservationSource.CHECKER,
        category="unsolved_goal",
        message="goal text",
        goal_fingerprint=fingerprint,
        raw_evidence_ref=f"attempt:{attempt}",
    )


def _stalled_branch(
    branch_id: str, obligation_id: str
) -> ProofBranch:
    return _branch(
        branch_id,
        obligation_id,
        observations=(
            _goal_obs(0, "fp-a"),
            _goal_obs(1, "fp-a"),
            _goal_obs(2, "fp-a"),
        ),
    )


def _accepted_fact(obligation_id: str, *, version: int = 1) -> VerifiedFact:
    return VerifiedFact(
        obligation_id=obligation_id,
        obligation_version=version,
        statement="lemma helper : True := by trivial",
        source_attempt_index=0,
        checker_category="proof_accepted",
        safety_accepted=True,
        declaration_id=obligation_id,
    )


def _multi_helper_workspace(
    *,
    helper_statuses: tuple[ObligationStatus, ObligationStatus],
    accepted_helper: bool = False,
    branches: tuple[ProofBranch, ...] = (),
):
    """Root depending on two helpers (hot + cold); statuses are parameterised."""
    base = initialize_from_task(_task())
    root_v1 = base.obligation_graph.by_id("sample")
    assert root_v1 is not None
    hot = ProofObligation(
        obligation_id="sample.hot",
        version=1,
        title="hot helper",
        lean_statement="lemma hot : True := by trivial",
        status=helper_statuses[0],
    )
    cold = ProofObligation(
        obligation_id="sample.cold",
        version=1,
        title="cold helper",
        lean_statement="lemma cold : True := by trivial",
        status=helper_statuses[1],
    )
    root_v2 = ProofObligation(
        obligation_id="sample",
        version=2,
        title="sample",
        lean_statement=root_v1.lean_statement,
        dependency_ids=("sample.hot", "sample.cold"),
        status=ObligationStatus.OPEN,
    )
    graph = ObligationGraph(
        obligations=(root_v1, root_v2, hot, cold),
        root_obligation_id="sample",
    )
    accepted_facts = (_accepted_fact("sample.hot"),) if accepted_helper else ()
    return base.successor(
        obligation_graph=graph,
        accepted_facts=accepted_facts,
        branches=branches,
    )


def _snapshot() -> BudgetSnapshot:
    return BudgetSnapshot(
        checks_used=0,
        model_calls_used=0,
        elapsed_seconds=1.0,
        remaining_checks=8,
        remaining_model_calls=4,
        exhausted_reason=None,
    )


def _hint(hints, obligation_id: str) -> ObligationBudgetHint:
    return next(h for h in hints if h.obligation_id == obligation_id)


class SoftEnvelopeTests(unittest.TestCase):
    def test_root_obligation_carries_root_bonus(self) -> None:
        workspace = _multi_helper_workspace(
            helper_statuses=(ObligationStatus.OPEN, ObligationStatus.OPEN)
        )
        hints = build_obligation_budget_hints(
            workspace, budget_snapshot=_snapshot()
        )
        root = _hint(hints, "sample")
        helper = _hint(hints, "sample.hot")
        cfg = BudgetHintDefaults()
        # Root: base + root_bonus (no inbound edges) = 2 checks.
        self.assertEqual(root.soft_checks, cfg.base_soft_checks + cfg.root_bonus_checks)
        self.assertEqual(
            root.soft_model_calls,
            cfg.base_soft_model_calls + cfg.root_bonus_model_calls,
        )
        # Helper: base + per_unlock * 1 (depended on by the root) = 2 checks.
        self.assertEqual(
            helper.soft_checks,
            cfg.base_soft_checks + cfg.per_unlock_bonus_checks,
        )

    def test_unlock_value_neighbour_recovers_budget(self) -> None:
        # When one helper is already accepted, the root (whose closure contains
        # the accepted helper) keeps a recovery bonus on top of its root bonus.
        workspace = _multi_helper_workspace(
            helper_statuses=(ObligationStatus.ACCEPTED, ObligationStatus.OPEN),
            accepted_helper=True,
        )
        hints = build_obligation_budget_hints(
            workspace, budget_snapshot=_snapshot()
        )
        root = _hint(hints, "sample")
        cfg = BudgetHintDefaults()
        self.assertEqual(
            root.soft_checks,
            cfg.base_soft_checks
            + cfg.root_bonus_checks
            + cfg.accepted_neighbor_bonus_checks,
        )

    def test_stalled_branch_envelope_collapses_to_zero(self) -> None:
        workspace = _seed().successor(
            branches=(_stalled_branch("sample.b1", "sample"),)
        )
        hints = build_obligation_budget_hints(
            workspace, budget_snapshot=_snapshot()
        )
        root = _hint(hints, "sample")
        cfg = BudgetHintDefaults()
        # Stalled *replaces* the envelope (base collapses to stalled_soft=0),
        # then the additive root_bonus still applies. No accepted neighbour on
        # a fresh seed, so no recovery bonus.
        self.assertEqual(
            root.soft_checks, cfg.stalled_soft_checks + cfg.root_bonus_checks
        )
        self.assertEqual(
            root.soft_model_calls,
            cfg.stalled_soft_model_calls + cfg.root_bonus_model_calls,
        )

    def test_derivation_never_mutates_workspace(self) -> None:
        workspace = _multi_helper_workspace(
            helper_statuses=(ObligationStatus.OPEN, ObligationStatus.OPEN)
        )
        version_before = workspace.version
        facts_before = workspace.accepted_facts
        build_obligation_budget_hints(workspace, budget_snapshot=_snapshot())
        self.assertEqual(workspace.version, version_before)
        self.assertEqual(workspace.accepted_facts, facts_before)


class BorrowJoinTests(unittest.TestCase):
    def test_borrowed_zero_when_under_soft_budget(self) -> None:
        hints = (
            ObligationBudgetHint("sample", soft_model_calls=2, soft_checks=2),
        )
        joined = join_borrowed_costs(
            hints, {"sample": {"checks": 1, "model_calls": 1}}
        )
        self.assertEqual(joined[0].borrowed_checks, 0)
        self.assertEqual(joined[0].borrowed_model_calls, 0)

    def test_borrowed_exceeds_when_direct_cost_over_soft(self) -> None:
        hints = (
            ObligationBudgetHint("sample", soft_model_calls=1, soft_checks=1),
        )
        joined = join_borrowed_costs(
            hints, {"sample": {"checks": 3, "model_calls": 2}}
        )
        self.assertEqual(joined[0].borrowed_checks, 2)
        self.assertEqual(joined[0].borrowed_model_calls, 1)

    def test_missing_cost_entry_borrows_nothing(self) -> None:
        hints = (
            ObligationBudgetHint("sample", soft_model_calls=1, soft_checks=1),
        )
        joined = join_borrowed_costs(hints, {})
        self.assertEqual(joined[0].borrowed_checks, 0)
        self.assertEqual(joined[0].borrowed_model_calls, 0)


class SerializationTests(unittest.TestCase):
    def test_to_dict_round_trips_through_from_dict(self) -> None:
        hint = ObligationBudgetHint(
            obligation_id="sample.hot",
            soft_model_calls=3,
            soft_checks=5,
            borrowed_model_calls=1,
            borrowed_checks=2,
        )
        round_trip = ObligationBudgetHint.from_dict(hint.to_dict())
        self.assertEqual(round_trip, hint)

    def test_to_dict_has_required_keys(self) -> None:
        hint = ObligationBudgetHint("sample", soft_model_calls=1, soft_checks=2)
        self.assertEqual(
            set(hint.to_dict()),
            {
                "obligation_id",
                "soft_model_calls",
                "soft_checks",
                "borrowed_model_calls",
                "borrowed_checks",
            },
        )


class EndToEndJoinTests(unittest.TestCase):
    """borrowed_* written to metadata must agree with cost_summary."""

    def _attempt(self, branch_id: str) -> AttemptRecord:
        edit = CandidateEdit(
            text="trivial",
            action="model_complete",
            metadata={"structured_branch_id": branch_id},
        )
        return AttemptRecord(
            attempt_index=0,
            candidate_id=f"{branch_id}.candidate",
            edit=edit,
            candidate_file=None,
            check_result=CheckResult(
                accepted=False,
                category=DiagnosticCategory.UNSOLVED_GOALS,
                raw_output="",
                elapsed_seconds=0.0,
            ),
        )

    def _usage(self, branch_id: str, obligation_id: str) -> dict:
        return {
            "input_tokens": 10,
            "output_tokens": 5,
            "structured_branch_id": branch_id,
            "structured_obligation_id": obligation_id,
        }

    def test_borrowed_agrees_with_cost_summary(self) -> None:
        workspace = _seed().successor(
            branches=(_branch("sample.b1", "sample"),)
        )
        attempts = (
            self._attempt("sample.b1"),
            self._attempt("sample.b1"),
            self._attempt("sample.b1"),
        )
        model_usage = (
            self._usage("sample.b1", "sample"),
            self._usage("sample.b1", "sample"),
            self._usage("sample.b1", "sample"),
        )
        summary = build_cost_summary(
            workspace=workspace,
            attempts=attempts,
            attempt_metrics=(),
            model_usage=model_usage,
            run_metrics=None,
            snapshot=_snapshot(),
            assembly_outcome=None,
        )
        obligation_direct = {
            entry["obligation_id"]: entry["direct_cost"]
            for entry in summary["obligations"]
        }
        hints = build_obligation_budget_hints(
            workspace, budget_snapshot=_snapshot()
        )
        joined = join_borrowed_costs(hints, obligation_direct)
        root = _hint(joined, "sample")

        direct = obligation_direct["sample"]
        # soft_checks for a non-stalled root = base + root_bonus = 2; spent 3.
        self.assertEqual(root.soft_checks, 2)
        self.assertEqual(
            root.borrowed_checks,
            max(0, direct["checks"] - root.soft_checks),
        )
        self.assertEqual(
            root.borrowed_model_calls,
            max(0, direct["model_calls"] - root.soft_model_calls),
        )
        # Every active obligation has both a cost entry and a hint.
        self.assertEqual(len(joined), len(summary["obligations"]))


class SingleRootInvariantTests(unittest.TestCase):
    """An overdraft must never flip a single-root run to PARTIAL/BLOCKED."""

    def test_overdraft_does_not_touch_finalize_status(self) -> None:
        # Soft-budget derivation is a pure projection; it does not consult or
        # influence finalize_workspace_status. Concretely: a workspace with one
        # OPEN root and a stalled (overdrafted) branch still finalises to
        # SEARCHING, never PARTIAL/BLOCKED, regardless of how big the envelope
        # overshoot is. The hint merely records the borrow.
        from agent.search.structured.run_state import finalize_workspace_status

        workspace = _seed().successor(
            branches=(_stalled_branch("sample.b1", "sample"),)
        )
        hints = build_obligation_budget_hints(
            workspace, budget_snapshot=_snapshot()
        )
        # Build a hint with a large borrow to stress the invariant.
        joined = join_borrowed_costs(
            hints, {"sample": {"checks": 50, "model_calls": 50}}
        )
        self.assertGreater(_hint(joined, "sample").borrowed_checks, 0)
        status = finalize_workspace_status(workspace, accepted=False)
        self.assertEqual(status, WorkspaceStatus.SEARCHING)


if __name__ == "__main__":
    unittest.main()
