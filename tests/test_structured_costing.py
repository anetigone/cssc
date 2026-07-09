"""Phase 8.1: structured branch/obligation cost attribution.

Covers :mod:`agent.search.structured.costing`: direct/transitive cost split per
branch and obligation, the assembly and run layers, serialization, and the
cost rules from ``tmp/phase8_plan.md`` §2 (structural actions = 0 checks /
0 model calls; capability = 1 check / 0 extra model calls; assembly is
run-level; transitive adds helper direct cost without double counting).
"""

from __future__ import annotations

import unittest

from agent.proof_system.assembler import AssemblyResult
from agent.proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ProofTask,
)
from agent.proof_system.workspace import (
    BranchStatus,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    initialize_from_task,
)
from agent.search.budget import BudgetSnapshot
from agent.search.controller.types import AttemptRecord
from agent.search.cost import CostVector, cost_vector_from_metrics_and_budget, to_dict
from agent.search.metrics import summarize_run
from agent.search.structured.costing import (
    BranchCostSummary,
    ObligationCostSummary,
    build_cost_summary,
)


def _task() -> ProofTask:
    return ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")


def _seed():
    return initialize_from_task(_task())


def _branch(
    branch_id: str,
    obligation_id: str,
    *,
    status: BranchStatus = BranchStatus.ACTIVE,
) -> ProofBranch:
    return ProofBranch(
        branch_id=branch_id,
        obligation_id=obligation_id,
        obligation_version=1,
        status=status,
    )


def _check(accepted: bool = False, elapsed: float = 0.0) -> CheckResult:
    return CheckResult(
        accepted=accepted,
        category=(
            DiagnosticCategory.PROOF_ACCEPTED
            if accepted
            else DiagnosticCategory.UNSOLVED_GOALS
        ),
        raw_output="",
        elapsed_seconds=elapsed,
    )


def _attempt(
    branch_id: str,
    *,
    action: str = "model_complete",
    elapsed: float = 0.0,
) -> AttemptRecord:
    edit = CandidateEdit(
        text="trivial",
        action=action,
        metadata={"structured_branch_id": branch_id},
    )
    return AttemptRecord(
        attempt_index=0,
        candidate_id=f"{branch_id}.candidate",
        edit=edit,
        candidate_file=None,
        check_result=_check(elapsed=elapsed),
    )


def _usage(
    branch_id: str,
    obligation_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "structured_branch_id": branch_id,
        "structured_obligation_id": obligation_id,
    }


def _snapshot() -> BudgetSnapshot:
    return BudgetSnapshot(
        checks_used=0,
        model_calls_used=0,
        elapsed_seconds=1.0,
        remaining_checks=0,
        remaining_model_calls=0,
        exhausted_reason=None,
    )


def _run_metrics(model_calls: int = 0, checks: int = 0):
    return summarize_run(
        sample_id="sample-1",
        task_id="sample",
        accepted=False,
        stop_reason="budget",
        attempts=(),
        budget_checks_used=checks,
        budget_model_calls_used=model_calls,
        budget_exhausted_reason=None,
    )


def _summary(workspace, **kwargs):
    """Call build_cost_summary with sensible defaults for the given workspace."""
    defaults = dict(
        attempts=(),
        attempt_metrics=(),
        model_usage=(),
        run_metrics=_run_metrics(),
        snapshot=_snapshot(),
        assembly_outcome=None,
    )
    defaults.update(kwargs)
    return build_cost_summary(workspace=workspace, **defaults)


def _by_branch(summary, branch_id: str) -> BranchCostSummary:
    raw = next(item for item in summary["branches"] if item["branch_id"] == branch_id)
    return BranchCostSummary.from_dict(raw)


def _obligation(summary, obligation_id: str) -> ObligationCostSummary:
    raw = next(
        item
        for item in summary["obligations"]
        if item["obligation_id"] == obligation_id
    )
    return ObligationCostSummary.from_dict(raw)


class BranchDirectCostTests(unittest.TestCase):
    def test_implement_path_counts_check_and_tagged_model_call(self) -> None:
        workspace = _seed().successor(
            branches=(_branch("sample.b1", "sample"),)
        )
        summary = _summary(
            workspace,
            attempts=(_attempt("sample.b1", elapsed=0.4),),
            model_usage=(_usage("sample.b1", "sample", input_tokens=120, output_tokens=30),),
        )
        branch = _by_branch(summary, "sample.b1")
        self.assertEqual(branch.attempts, 1)
        self.assertEqual(branch.direct_cost.checks, 1)
        self.assertEqual(branch.direct_cost.model_calls, 1)
        self.assertEqual(branch.direct_cost.input_tokens, 120)
        self.assertEqual(branch.direct_cost.output_tokens, 30)
        self.assertEqual(branch.direct_cost.elapsed_ms, 400)
        # No helpers -> transitive equals direct.
        self.assertEqual(branch.transitive_cost, branch.direct_cost)

    def test_capability_audit_is_one_check_no_extra_model_call(self) -> None:
        # The capability audit reuses the iteration's reserved call, so the
        # single tagged model_usage entry is the popped branch's call: checks=1,
        # model_calls=1 (the audit itself adds 0 extra calls).
        workspace = _seed().successor(
            branches=(_branch("sample.b1", "sample"),)
        )
        summary = _summary(
            workspace,
            attempts=(_attempt("sample.b1", action="capability_test"),),
            model_usage=(_usage("sample.b1", "sample"),),
        )
        branch = _by_branch(summary, "sample.b1")
        self.assertEqual(branch.direct_cost.checks, 1)
        self.assertEqual(branch.direct_cost.model_calls, 1)

    def test_decompose_only_branch_has_zero_direct_cost(self) -> None:
        # Structural actions create no AttemptRecord and no tagged usage, so a
        # branch that only decomposed contributes 0/0.
        workspace = _seed().successor(
            branches=(_branch("sample.b1", "sample"),)
        )
        summary = _summary(workspace)
        branch = _by_branch(summary, "sample.b1")
        self.assertEqual(branch.direct_cost.checks, 0)
        self.assertEqual(branch.direct_cost.model_calls, 0)
        self.assertEqual(branch.attempts, 0)

    def test_status_flags_read_from_workspace_branches(self) -> None:
        workspace = _seed().successor(
            branches=(
                _branch("sample.b1", "sample", status=BranchStatus.ACCEPTED),
                _branch("sample.b2", "sample", status=BranchStatus.DORMANT),
                _branch("sample.b3", "sample", status=BranchStatus.BLOCKED),
            )
        )
        summary = _summary(workspace)
        self.assertTrue(_by_branch(summary, "sample.b1").accepted)
        self.assertTrue(_by_branch(summary, "sample.b2").dormant)
        self.assertTrue(_by_branch(summary, "sample.b3").blocked)


class TransitiveCostTests(unittest.TestCase):
    def _helper(self, obligation_id: str) -> ProofObligation:
        return ProofObligation(
            obligation_id=obligation_id,
            version=1,
            title=obligation_id,
            lean_statement=f"lemma {obligation_id} : True := by trivial",
            status=ObligationStatus.ACCEPTED,
        )

    def test_obligation_transitive_includes_helper_direct(self) -> None:
        workspace = _seed().decompose("sample", [self._helper("sample.helper1")])
        workspace = workspace.successor(
            branches=(
                _branch("sample.b1", "sample"),
                _branch("sample.helper1.b1", "sample.helper1"),
            )
        )
        summary = _summary(
            workspace,
            attempts=(_attempt("sample.b1", elapsed=0.1), _attempt("sample.helper1.b1", elapsed=0.2)),
            model_usage=(_usage("sample.b1", "sample"), _usage("sample.helper1.b1", "sample.helper1")),
        )
        root = _obligation(summary, "sample")
        helper = _obligation(summary, "sample.helper1")
        # Helper has no further dependencies -> transitive == direct.
        self.assertEqual(helper.transitive_cost, helper.direct_cost)
        # Root depends on the helper, so its transitive includes helper direct.
        self.assertEqual(
            root.transitive_cost.checks,
            root.direct_cost.checks + helper.direct_cost.checks,
        )
        self.assertGreater(root.transitive_cost.checks, root.direct_cost.checks)

    def test_branch_transitive_walks_dependency_closure(self) -> None:
        # root -> helper1 -> helper2 chain via two decomposes.
        workspace = _seed().decompose("sample", [self._helper("sample.helper1")])
        workspace = workspace.decompose(
            "sample.helper1", [self._helper("sample.helper2")]
        )
        workspace = workspace.successor(
            branches=(
                _branch("sample.b1", "sample"),
                _branch("sample.helper1.b1", "sample.helper1"),
                _branch("sample.helper2.b1", "sample.helper2"),
            )
        )
        summary = _summary(
            workspace,
            attempts=(
                _attempt("sample.b1"),
                _attempt("sample.helper1.b1"),
                _attempt("sample.helper2.b1"),
            ),
        )
        root_branch = _by_branch(summary, "sample.b1")
        helper1_branch = _by_branch(summary, "sample.helper1.b1")
        helper2_direct = _by_branch(summary, "sample.helper2.b1").direct_cost
        # Root branch closure reaches both helpers.
        self.assertEqual(root_branch.transitive_cost.checks, 3)
        # Helper1 closure reaches helper2 but not root.
        self.assertEqual(
            helper1_branch.transitive_cost.checks,
            helper1_branch.direct_cost.checks + helper2_direct.checks,
        )


class AssemblyAndRunLayerTests(unittest.TestCase):
    def test_assembly_none_when_outcome_missing(self) -> None:
        workspace = _seed().successor(branches=(_branch("sample.b1", "sample"),))
        self.assertIsNone(_summary(workspace)["assembly"])

    def test_assembly_layer_one_check_no_model_call(self) -> None:
        workspace = _seed().successor(branches=(_branch("sample.b1", "sample"),))
        outcome = AssemblyResult(
            accepted=True,
            source="",
            check_result=_check(accepted=True, elapsed=0.5),
        )
        summary = _summary(workspace, assembly_outcome=outcome)
        self.assertEqual(summary["assembly"]["checks"], 1)
        self.assertEqual(summary["assembly"]["model_calls"], 0)
        self.assertEqual(summary["assembly"]["elapsed_ms"], 500)

    def test_run_layer_matches_metadata_cost_projection(self) -> None:
        workspace = _seed().successor(branches=(_branch("sample.b1", "sample"),))
        snapshot = _snapshot()
        metrics = _run_metrics(model_calls=3, checks=5)
        summary = _summary(workspace, run_metrics=metrics, snapshot=snapshot)
        self.assertEqual(
            summary["run"],
            to_dict(cost_vector_from_metrics_and_budget(metrics, snapshot)),
        )


class SerializationAndEdgeCasesTests(unittest.TestCase):
    def test_branch_and_obligation_round_trip(self) -> None:
        branch = BranchCostSummary(
            branch_id="sample.b1",
            obligation_id="sample",
            direct_cost=CostVector(
                checks=2, model_calls=1, input_tokens=10, output_tokens=4, elapsed_ms=80
            ),
            transitive_cost=CostVector(checks=3, elapsed_ms=120),
            attempts=2,
            accepted=True,
            blocked=False,
            dormant=False,
        )
        rebuilt = BranchCostSummary.from_dict(branch.to_dict())
        self.assertEqual(rebuilt, branch)

        obligation = ObligationCostSummary(
            obligation_id="sample",
            direct_cost=branch.direct_cost,
            transitive_cost=branch.transitive_cost,
            branch_ids=("sample.b1",),
        )
        self.assertEqual(
            ObligationCostSummary.from_dict(obligation.to_dict()),
            obligation,
        )

    def test_empty_state_does_not_crash(self) -> None:
        workspace = _seed()
        summary = _summary(workspace)
        self.assertEqual(summary["branches"], ())
        self.assertEqual(summary["obligations"][0]["direct_cost"]["checks"], 0)
        self.assertIn("run", summary)
        self.assertIsNone(summary["assembly"])

    def test_untagged_model_usage_attributed_to_no_branch(self) -> None:
        # A usage dict lacking branch keys (pre-tagging or stray) still flows
        # into the run-level token sum but is not charged to any branch.
        workspace = _seed().successor(branches=(_branch("sample.b1", "sample"),))
        untagged = {"input_tokens": 99, "output_tokens": 1}
        snapshot = BudgetSnapshot(
            checks_used=0,
            model_calls_used=1,
            elapsed_seconds=1.0,
            remaining_checks=0,
            remaining_model_calls=0,
            exhausted_reason=None,
        )
        summary = _summary(
            workspace,
            model_usage=(untagged,),
            run_metrics=_run_metrics(model_calls=1),
            snapshot=snapshot,
        )
        branch = _by_branch(summary, "sample.b1")
        self.assertEqual(branch.direct_cost.model_calls, 0)
        self.assertEqual(branch.direct_cost.input_tokens, 0)
        # But the run layer still counts it.
        self.assertEqual(summary["run"]["model_calls"], 1)

    def test_blocked_after_empty_proposals_branch_reports_zero(self) -> None:
        # A branch blocked after empty proposals has no checked attempt, but the
        # controller still reserved one generator call for that branch.
        workspace = _seed().successor(
            branches=(_branch("sample.b1", "sample", status=BranchStatus.BLOCKED),)
        )
        snapshot = BudgetSnapshot(
            checks_used=0,
            model_calls_used=1,
            elapsed_seconds=1.0,
            remaining_checks=0,
            remaining_model_calls=0,
            exhausted_reason=None,
        )
        summary = _summary(
            workspace,
            model_usage=(_usage("sample.b1", "sample"),),
            run_metrics=_run_metrics(model_calls=1),
            snapshot=snapshot,
        )
        branch = _by_branch(summary, "sample.b1")
        self.assertEqual(branch.direct_cost.model_calls, 1)
        self.assertEqual(branch.direct_cost.checks, 0)
        self.assertEqual(summary["run"]["model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
