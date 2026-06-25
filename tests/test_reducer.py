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
    ObligationStatus,
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
    apply_decompose,
)
from agent.search.structured.proposal import DecomposeChildSpec

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


CAPABILITY_MUTATIONS = DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.RUN_CAPABILITY_TEST]


def _capability_action(branch_id: str = "root-branch") -> SearchAction:
    return SearchAction(
        kind=SearchActionKind.RUN_CAPABILITY_TEST,
        target_branch_id=branch_id,
        allowed_mutations=CAPABILITY_MUTATIONS,
        rationale="probe tactic#simp availability",
    )


def _check_result(
    *,
    accepted: bool,
    category: DiagnosticCategory,
    message: str = "",
) -> CheckResult:
    feedback = ParsedFeedback(category=category, message=message)
    return CheckResult(
        accepted=accepted,
        category=category,
        raw_output=message,
        parsed_feedback=feedback,
    )


class ReducerCapabilityAuditTests(unittest.TestCase):
    """Phase 7.3: RUN_CAPABILITY_TEST folds into an observation and may block."""

    def test_missing_capability_blocks_branch_and_obligation(self) -> None:
        workspace = _seed_workspace()
        original = workspace
        action = _capability_action()

        workspace = apply(
            workspace,
            action,
            _result(
                check_result=_check_result(
                    accepted=False,
                    category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
                    message="unknown identifier 'simp'",
                ),
                attempt_index=2,
            ),
        )

        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.BLOCKED)
        self.assertIsNone(branch.lean_artifact)
        self.assertEqual(branch.last_action, action)

        # The obligation is blocked together with the branch — no gap.
        obligation = workspace.obligation_graph.by_id("sample")
        assert obligation is not None
        self.assertEqual(obligation.status, ObligationStatus.BLOCKED)

        # A capability-audit observation is recorded with the right source.
        cap_obs = [
            o for o in branch.observations
            if o.source.value == "capability_audit"
        ]
        self.assertEqual(len(cap_obs), 1)
        self.assertEqual(cap_obs[0].category, "unknown_identifier")
        self.assertEqual(cap_obs[0].raw_evidence_ref, "capability:2")
        self.assertIn("simp", cap_obs[0].message)

        # Immutability: the input workspace is untouched.
        self.assertNotEqual(workspace.version, original.version)

    def test_available_capability_stays_active_with_observation(self) -> None:
        workspace = _seed_workspace()
        action = _capability_action()

        workspace = apply(
            workspace,
            action,
            _result(
                check_result=_check_result(
                    accepted=True,
                    category=DiagnosticCategory.PROOF_ACCEPTED,
                    message="simp compiled",
                ),
                attempt_index=1,
            ),
        )

        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.ACTIVE)
        # A capability probe being accepted does NOT register a verified fact —
        # the proposition is not proven, only the tactic exists.
        self.assertEqual(workspace.accepted_facts, ())
        obligation = workspace.obligation_graph.by_id("sample")
        assert obligation is not None
        self.assertEqual(obligation.status, ObligationStatus.OPEN)

        cap_obs = [
            o for o in branch.observations
            if o.source.value == "capability_audit"
        ]
        self.assertEqual(len(cap_obs), 1)
        self.assertIn("available", cap_obs[0].message)

    def test_non_missing_failure_does_not_block(self) -> None:
        # UNSOLVED_GOALS is an implementation problem, not a missing capability:
        # the audit records evidence but leaves the route open for IMPLEMENT.
        workspace = _seed_workspace()
        action = _capability_action()

        workspace = apply(
            workspace,
            action,
            _result(
                check_result=_check_result(
                    accepted=False,
                    category=DiagnosticCategory.UNSOLVED_GOALS,
                    message="unsolved goals",
                ),
                attempt_index=0,
            ),
        )

        branch = next(b for b in workspace.branches if b.branch_id == "root-branch")
        self.assertEqual(branch.status, BranchStatus.ACTIVE)
        obligation = workspace.obligation_graph.by_id("sample")
        assert obligation is not None
        self.assertEqual(obligation.status, ObligationStatus.OPEN)
        cap_obs = [
            o for o in branch.observations
            if o.source.value == "capability_audit"
        ]
        self.assertEqual(len(cap_obs), 1)

    def test_missing_capability_does_not_mutate_input(self) -> None:
        workspace = _seed_workspace()
        snapshot_version = workspace.version
        snapshot_obligation = workspace.obligation_graph.by_id("sample")
        assert snapshot_obligation is not None
        snapshot_obligation_status = snapshot_obligation.status

        apply(
            workspace,
            _capability_action(),
            _result(
                check_result=_check_result(
                    accepted=False,
                    category=DiagnosticCategory.INVALID_REFERENCE,
                ),
            ),
        )

        self.assertEqual(workspace.version, snapshot_version)
        self.assertEqual(
            workspace.obligation_graph.by_id("sample").status,
            snapshot_obligation_status,
        )


def _decompose_action(branch_id: str = "root-branch") -> SearchAction:
    return SearchAction(
        kind=SearchActionKind.DECOMPOSE,
        target_branch_id=branch_id,
        allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.DECOMPOSE],
        rationale="decompose root into helpers",
    )


def _helpers() -> list[DecomposeChildSpec]:
    return [
        DecomposeChildSpec(
            child_id="sample.helper1",
            statement="lemma helper1 : True := by trivial",
        ),
        DecomposeChildSpec(
            child_id="sample.helper2",
            statement="lemma helper2 : True := by trivial",
        ),
    ]


class ReducerDecomposeTests(unittest.TestCase):
    def test_decompose_supersedes_parent_and_seeds_child_branches(self) -> None:
        workspace = _seed_workspace()
        original = workspace

        workspace = apply_decompose(
            workspace,
            _decompose_action(),
            children=_helpers(),
            parent_branch_id="root-branch",
        )

        # The workspace validates immediately — the old parent-version branch
        # must have been retired to SUPERSEDED in the same successor.
        self.assertTrue(workspace.validate().ok)

        # Old root branch retired; new parent-v2 branch + two child branches.
        old_root = next(
            b for b in workspace.branches if b.branch_id == "root-branch"
        )
        self.assertEqual(old_root.status, BranchStatus.SUPERSEDED)
        new_parent_branches = [
            b
            for b in workspace.branches
            if b.obligation_id == "sample" and b.status == BranchStatus.ACTIVE
        ]
        self.assertEqual(len(new_parent_branches), 1)
        self.assertEqual(new_parent_branches[0].obligation_version, 2)
        child_branches = sorted(
            (b for b in workspace.branches if b.obligation_id.startswith("sample.helper")),
            key=lambda b: b.branch_id,
        )
        self.assertEqual(len(child_branches), 2)
        for child in child_branches:
            self.assertEqual(child.status, BranchStatus.ACTIVE)
            self.assertEqual(child.obligation_version, 1)

        # The root obligation now depends on both helpers.
        root = workspace.obligation_graph.by_id("sample")
        assert root is not None
        self.assertEqual(root.version, 2)
        self.assertIn("sample.helper1", root.dependency_ids)
        self.assertIn("sample.helper2", root.dependency_ids)

        # Immutability.
        self.assertIsNot(workspace, original)
        self.assertGreater(workspace.version, original.version)

    def test_decompose_no_op_on_empty_children(self) -> None:
        workspace = _seed_workspace()
        result = apply_decompose(
            workspace,
            _decompose_action(),
            children=[],
            parent_branch_id="root-branch",
        )
        self.assertIs(result, workspace)

    def test_decompose_skips_stale_parent_version(self) -> None:
        # Decompose once, then try to decompose the (now superseded) old branch
        # again — it pins v1 while the current obligation is v2, so the second
        # decompose is a no-op.
        workspace = _seed_workspace()
        workspace = apply_decompose(
            workspace,
            _decompose_action(),
            children=_helpers(),
            parent_branch_id="root-branch",
        )
        version_before = workspace.version
        result = apply_decompose(
            workspace,
            _decompose_action(),
            children=_helpers(),
            parent_branch_id="root-branch",
        )
        self.assertEqual(result.version, version_before)


class ReducerArtifactContractTests(unittest.TestCase):
    """Phase 7.4: root vs helper artifact kind and fact statement."""

    def _decomposed_workspace(self):
        workspace = _seed_workspace()
        return apply_decompose(
            workspace,
            _decompose_action(),
            children=_helpers(),
            parent_branch_id="root-branch",
        )

    def _accept_branch(
        self,
        workspace,
        branch_id,
        *,
        proof_text="trivial",
        source=None,
    ):
        return apply(
            workspace,
            SearchAction(
                kind=SearchActionKind.IMPLEMENT,
                target_branch_id=branch_id,
                allowed_mutations=IMPLEMENT_MUTATIONS,
                rationale="implement",
            ),
            StructuredActionResult(
                branch_id=branch_id,
                check_result=_accepted_check(),
                safety_verdict=SafetyVerdict(accepted=True),
                proof_text=proof_text,
                source=source if source is not None else proof_text,
                attempt_index=0,
            ),
        )

    def test_helper_fact_statement_is_the_rendered_declaration(self) -> None:
        workspace = self._decomposed_workspace()
        helper_branch = next(
            b for b in workspace.branches if b.obligation_id == "sample.helper1"
        )
        # The controller renders a helper as its full declaration (statement
        # template with the proof body in the hole); the reducer mirrors that
        # rendered source as the fact statement so a parent proof can reuse the
        # helper by name. Here we pass the rendered declaration directly.
        rendered = "lemma helper1 : True := by trivial"
        workspace = self._accept_branch(
            workspace,
            helper_branch.branch_id,
            proof_text="trivial",
            source=rendered,
        )

        helper_fact = next(
            f for f in workspace.accepted_facts if f.obligation_id == "sample.helper1"
        )
        self.assertEqual(helper_fact.statement, rendered)
        self.assertEqual(helper_fact.artifact_source, rendered)

    def test_helper_artifact_kind_is_declaration(self) -> None:
        workspace = self._decomposed_workspace()
        helper_branch = next(
            b for b in workspace.branches if b.obligation_id == "sample.helper1"
        )
        workspace = self._accept_branch(workspace, helper_branch.branch_id)
        helper_branch = next(
            b for b in workspace.branches if b.obligation_id == "sample.helper1"
        )
        assert helper_branch.lean_artifact is not None
        self.assertEqual(helper_branch.lean_artifact.kind.value, "declaration")

    def test_root_artifact_kind_is_proof_body(self) -> None:
        # Baseline: the single-root accept path still produces a PROOF_BODY
        # artifact and a proof-body fact statement.
        workspace = _seed_workspace()
        workspace = self._accept_branch(workspace, "root-branch", proof_text="trivial")
        root_branch = next(
            b for b in workspace.branches if b.branch_id == "root-branch"
        )
        assert root_branch.lean_artifact is not None
        self.assertEqual(root_branch.lean_artifact.kind.value, "proof_body")
        root_fact = workspace.accepted_facts[0]
        self.assertEqual(root_fact.statement, "trivial")


if __name__ == "__main__":
    unittest.main()
