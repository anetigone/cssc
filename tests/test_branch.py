from __future__ import annotations

import unittest

from agent.proof_system.base import DiagnosticCategory, ProgressSignal
from agent.proof_system.workspace.alignment import AlignmentLink, AlignmentRelation
from agent.proof_system.workspace.argument import ArgumentGraph, ArgumentStep
from agent.proof_system.workspace.artifact import LeanArtifact
from agent.proof_system.workspace.branch import (
    BranchStatus,
    ProofBranch,
    proof_branch_from_dict,
)
from agent.proof_system.workspace.observation import (
    Observation,
    ObservationSource,
)


class ProofBranchSerializationTest(unittest.TestCase):
    def test_minimal_round_trip(self) -> None:
        branch = ProofBranch(
            branch_id="b1",
            obligation_id="root",
            obligation_version=1,
        )
        restored = proof_branch_from_dict(branch.to_dict())
        self.assertEqual(restored, branch)
        self.assertEqual(restored.status, BranchStatus.ACTIVE)
        self.assertIsNone(restored.parent_branch_id)
        self.assertIsNone(restored.lean_artifact)

    def test_full_round_trip(self) -> None:
        branch = ProofBranch(
            branch_id="b2",
            obligation_id="helper",
            obligation_version=2,
            parent_branch_id="b1",
            argument=ArgumentGraph(
                steps=(
                    ArgumentStep(step_id="s1", claim="claim", introduced_fact_ids=("f1",)),
                )
            ),
            lean_artifact=LeanArtifact(
                source="theorem helper : True := by trivial",
                obligation_id="helper",
                obligation_version=2,
                declaration_id="helper",
                proof_body="trivial",
            ),
            alignment=(
                AlignmentLink(
                    argument_step_id="s1",
                    lean_declaration_id="helper",
                    relation=AlignmentRelation.IMPLEMENTS,
                ),
            ),
            observations=(
                Observation(
                    observation_id="attempt:0:goal:0",
                    source=ObservationSource.CHECKER,
                    category=DiagnosticCategory.UNSOLVED_GOALS.value,
                    message="unsolved",
                    raw_evidence_ref="attempt:0",
                ),
            ),
            last_action_summary="implemented helper",
            progress=ProgressSignal(
                accepted_prefix_chars=5,
                diagnostic_category=DiagnosticCategory.UNSOLVED_GOALS,
            ),
            status=BranchStatus.DORMANT,
        )
        restored = proof_branch_from_dict(branch.to_dict())
        self.assertEqual(restored, branch)

    def test_status_enum_values(self) -> None:
        for status in BranchStatus:
            self.assertEqual(
                proof_branch_from_dict(
                    {
                        "branch_id": "b",
                        "obligation_id": "root",
                        "obligation_version": 1,
                        "status": status.value,
                    }
                ).status,
                status,
            )


class ProofBranchValidationTest(unittest.TestCase):
    def test_valid_branch_with_explicit_unaligned_step(self) -> None:
        branch = ProofBranch(
            branch_id="b1",
            obligation_id="root",
            obligation_version=1,
            argument=ArgumentGraph(
                steps=(ArgumentStep(step_id="s1", claim="claim"),)
            ),
            alignment=(AlignmentLink(argument_step_id="s1"),),
        )

        self.assertTrue(branch.validate().ok, branch.validate().errors)

    def test_artifact_pin_must_match_branch(self) -> None:
        branch = ProofBranch(
            branch_id="b1",
            obligation_id="root",
            obligation_version=1,
            lean_artifact=LeanArtifact(
                source="theorem helper : True := by trivial",
                obligation_id="helper",
                obligation_version=2,
            ),
        )

        report = branch.validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("artifact is pinned" in error for error in report.errors))

    def test_each_argument_step_requires_explicit_alignment(self) -> None:
        branch = ProofBranch(
            branch_id="b1",
            obligation_id="root",
            obligation_version=1,
            argument=ArgumentGraph(
                steps=(ArgumentStep(step_id="s1", claim="claim"),)
            ),
        )

        report = branch.validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("explicit unaligned" in error for error in report.errors))

    def test_alignment_target_must_be_consistent_with_relation(self) -> None:
        step = ArgumentStep(step_id="s1", claim="claim")
        unaligned_with_target = ProofBranch(
            branch_id="b1",
            obligation_id="root",
            obligation_version=1,
            argument=ArgumentGraph(steps=(step,)),
            alignment=(
                AlignmentLink(
                    argument_step_id="s1",
                    lean_declaration_id="root",
                    relation=AlignmentRelation.UNALIGNED,
                ),
            ),
        )
        implements_without_target = ProofBranch(
            branch_id="b2",
            obligation_id="root",
            obligation_version=1,
            argument=ArgumentGraph(steps=(step,)),
            alignment=(
                AlignmentLink(
                    argument_step_id="s1",
                    relation=AlignmentRelation.IMPLEMENTS,
                ),
            ),
        )

        self.assertFalse(unaligned_with_target.validate().ok)
        self.assertFalse(implements_without_target.validate().ok)


if __name__ == "__main__":
    unittest.main()
