from __future__ import annotations

import unittest
from dataclasses import replace

from agent.proof_system.base import (
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
    ProofTask,
)
from agent.proof_system.workspace import (
    BranchStatus,
    DEFAULT_ALLOWED_MUTATIONS,
    ProofBranch,
    SearchAction,
    SearchActionKind,
    initialize_from_task,
)
from agent.search.safety import SafetyVerdict
from agent.search.structured.reducer import (
    REPAIR_THRESHOLD,
    StructuredActionResult,
    apply,
)

IMPLEMENT_MUTATIONS = DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.IMPLEMENT]


def _implement_action(branch_id: str = "root-branch") -> SearchAction:
    return SearchAction(
        kind=SearchActionKind.IMPLEMENT,
        target_branch_id=branch_id,
        allowed_mutations=IMPLEMENT_MUTATIONS,
        rationale="implement root",
    )


def _rejected_check(
    goal_fingerprint: str = "fp-a",
    *,
    message: str = "unsolved",
) -> CheckResult:
    feedback = ParsedFeedback(
        category=DiagnosticCategory.UNSOLVED_GOALS,
        message=message,
        goal_state=(GoalState(text=goal_fingerprint, goal_fingerprint=goal_fingerprint),),
    )
    return CheckResult(
        accepted=False,
        category=DiagnosticCategory.UNSOLVED_GOALS,
        raw_output=message,
        parsed_feedback=feedback,
    )


def _accepted_check() -> CheckResult:
    return CheckResult(
        accepted=True,
        category=DiagnosticCategory.PROOF_ACCEPTED,
        raw_output="no errors",
    )


def _result(
    *,
    branch_id: str = "root-branch",
    check_result: CheckResult,
    safety_verdict: SafetyVerdict | None = None,
    attempt_index: int = 0,
) -> StructuredActionResult:
    if safety_verdict is None:
        safety_verdict = SafetyVerdict(accepted=check_result.accepted)
    return StructuredActionResult(
        branch_id=branch_id,
        check_result=check_result,
        safety_verdict=safety_verdict,
        proof_text="trivial",
        source="theorem sample : True := by trivial",
        attempt_index=attempt_index,
    )


def _seed_workspace(
    *, branch_id: str = "root-branch", status: BranchStatus = BranchStatus.ACTIVE
):
    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    workspace = initialize_from_task(task)
    branch = ProofBranch(
        branch_id=branch_id,
        obligation_id="sample",
        obligation_version=1,
        status=status,
    )
    return workspace.successor(branches=(branch,))


class ReducerAcceptTests(unittest.TestCase):
    def test_accepted_and_safe_marks_branch_and_obligation_accepted(self) -> None:
        workspace = _seed_workspace()
        original = workspace
        action = _implement_action()

        workspace = apply(
            workspace,
            action,
            _result(
                check_result=_accepted_check(),
                safety_verdict=SafetyVerdict(accepted=True),
                attempt_index=3,
            ),
        )

        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.ACCEPTED)
        self.assertIsNotNone(branch.lean_artifact)
        self.assertEqual(branch.last_action, action)

        obligation = workspace.obligation_graph.by_id("sample")
        assert obligation is not None
        from agent.proof_system.workspace.obligation import ObligationStatus

        self.assertEqual(obligation.status, ObligationStatus.ACCEPTED)
        # VerifiedFact carries the source attempt and obligation version.
        self.assertEqual(len(workspace.accepted_facts), 1)
        self.assertEqual(workspace.accepted_facts[0].source_attempt_index, 3)

        # Immutability: the original workspace object is untouched. The accept
        # path bumps the version at least once (branch update + fact
        # registration); what matters is the input object is never mutated.
        self.assertIsNot(workspace, original)
        self.assertGreater(workspace.version, original.version)

    def test_accepted_branch_workspace_validates(self) -> None:
        workspace = _seed_workspace()
        workspace = apply(
            workspace,
            _implement_action(),
            _result(
                check_result=_accepted_check(),
                safety_verdict=SafetyVerdict(accepted=True),
            ),
        )
        self.assertTrue(workspace.validate().ok)


class ReducerFailureTests(unittest.TestCase):
    def test_rejected_check_appends_observations_keeps_active(self) -> None:
        workspace = _seed_workspace()
        branch_before = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch_before.observations, ())

        workspace = apply(
            workspace,
            _implement_action(),
            _result(check_result=_rejected_check("fp-a"), safety_verdict=SafetyVerdict(accepted=False)),
        )

        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.ACTIVE)
        self.assertEqual(len(branch.observations), 1)
        self.assertEqual(branch.observations[0].goal_fingerprint, "fp-a")
        # Failed realization is retained as provenance, not dropped.
        self.assertIsNotNone(branch.lean_artifact)

    def test_safety_rejected_appends_safety_observation(self) -> None:
        workspace = _seed_workspace()
        workspace = apply(
            workspace,
            _implement_action(),
            _result(
                check_result=_accepted_check(),
                safety_verdict=SafetyVerdict(accepted=False, reasons=("residual sorry",)),
            ),
        )

        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.ACTIVE)
        self.assertEqual(len(branch.observations), 1)
        self.assertEqual(branch.observations[0].category, "safety_rejected")
        self.assertIn("residual sorry", branch.observations[0].message)

    def test_branch_goes_dormant_after_stall_threshold(self) -> None:
        from agent.search.structured.frontier import STALL_THRESHOLD

        workspace = _seed_workspace()
        action = _implement_action()
        # Pump REPAIR_THRESHOLD identical-fingerprint failures; the branch is
        # still ACTIVE until STALL_THRESHOLD is reached.
        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=0)
        )
        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=1)
        )
        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        # After two identical failures the repair child spawns; the parent is
        # not dormant yet (stalled_streak == 2 < STALL_THRESHOLD == 3).
        self.assertEqual(branch.status, BranchStatus.ACTIVE)
        self.assertEqual(REPAIR_THRESHOLD, 2)
        self.assertEqual(STALL_THRESHOLD, 3)

        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=2)
        )
        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.DORMANT)


class ReducerRepairChildTests(unittest.TestCase):
    def test_repair_child_spawns_after_threshold(self) -> None:
        workspace = _seed_workspace()
        action = _implement_action()

        # First failure: parent only.
        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=0)
        )
        ids = [b.branch_id for b in workspace.branches]
        self.assertEqual(ids, ["root-branch"])

        # Second identical failure: a repair child spawns.
        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=1)
        )
        ids = sorted(b.branch_id for b in workspace.branches)
        self.assertEqual(ids, ["root-branch", "root-branch.r0"])

        child = next(b for b in workspace.branches if b.branch_id == "root-branch.r0")
        self.assertEqual(child.parent_branch_id, "root-branch")
        self.assertEqual(child.status, BranchStatus.ACTIVE)
        # Child inherits the evidence so far but starts without an artifact.
        self.assertEqual(len(child.observations), 2)
        self.assertIsNone(child.lean_artifact)

    def test_repair_child_inherits_argument_and_alignment(self) -> None:
        from agent.proof_system.workspace.argument import ArgumentGraph, ArgumentStep

        workspace = _seed_workspace()
        argument = ArgumentGraph(
            steps=(ArgumentStep(step_id="s1", claim="claim"),)
        )
        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        workspace = workspace.successor(
            branches=(replace(branch, argument=argument),)
        )

        action = _implement_action()
        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=0)
        )
        workspace = apply(
            workspace, action, _result(check_result=_rejected_check("fp-a"), attempt_index=1)
        )

        child = next(b for b in workspace.branches if b.branch_id == "root-branch.r0")
        self.assertEqual(child.argument, argument)


class ReducerImmutabilityTests(unittest.TestCase):
    def test_original_branch_tuple_is_not_mutated(self) -> None:
        workspace = _seed_workspace()
        original_branches = workspace.branches
        original_branch = workspace.branches[0]

        _ = apply(
            workspace,
            _implement_action(),
            _result(check_result=_rejected_check("fp-a")),
        )

        # The input tuple and branch object are byte-for-byte unchanged.
        self.assertEqual(original_branches, workspace.branches)
        self.assertEqual(original_branch.observations, ())
        self.assertEqual(original_branch.status, BranchStatus.ACTIVE)

    def test_unknown_branch_is_noop(self) -> None:
        workspace = _seed_workspace()
        result = apply(
            workspace,
            _implement_action(branch_id="ghost"),
            _result(
                branch_id="ghost",
                check_result=_rejected_check("fp-a"),
            ),
        )
        self.assertEqual(result, workspace)


if __name__ == "__main__":
    unittest.main()
