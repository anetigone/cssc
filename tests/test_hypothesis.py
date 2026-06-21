from __future__ import annotations

import unittest

from agent.proof_system.workspace.action import MutationKind, SearchAction, SearchActionKind
from agent.proof_system.workspace.hypothesis import (
    FailureHypothesis,
    FailureHypothesisReport,
    FailureKind,
    failure_hypothesis_from_dict,
)


def _action() -> SearchAction:
    return SearchAction(
        kind=SearchActionKind.REPAIR_IMPLEMENTATION,
        target_branch_id="b1",
        allowed_mutations=(MutationKind.LEAN_ARTIFACT,),
        rationale="test repair",
    )


class FailureHypothesisSerializationTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        hypothesis = FailureHypothesis(
            hypothesis_id="h1",
            kind=FailureKind.THEOREM_MISUSE,
            confidence=0.45,
            evidence_ids=("attempt:2:goal:0",),
            affected_step_ids=("s3",),
            proposed_tests=(_action(),),
        )
        restored = failure_hypothesis_from_dict(hypothesis.to_dict())
        self.assertEqual(restored, hypothesis)

    def test_round_trip_minimal(self) -> None:
        # affected_step_ids and proposed_tests may both be empty.
        hypothesis = FailureHypothesis(
            hypothesis_id="h2",
            kind=FailureKind.CAPABILITY_MISSING,
            confidence=0.2,
            evidence_ids=("attempt:1:summary",),
        )
        restored = failure_hypothesis_from_dict(hypothesis.to_dict())
        self.assertEqual(restored, hypothesis)


class FailureHypothesisValidateTest(unittest.TestCase):
    def _ok(
        self,
        *,
        evidence_ids: tuple[str, ...] = ("o1",),
        affected_step_ids: tuple[str, ...] = (),
        proposed_tests: tuple[SearchAction, ...] = (),
        confidence: float = 0.5,
        kind: FailureKind = FailureKind.ARGUMENT_GAP,
        hypothesis_id: str = "h1",
    ) -> FailureHypothesis:
        return FailureHypothesis(
            hypothesis_id=hypothesis_id,
            kind=kind,
            confidence=confidence,
            evidence_ids=evidence_ids,
            affected_step_ids=affected_step_ids,
            proposed_tests=proposed_tests,
        )

    def test_valid_minimal(self) -> None:
        self.assertTrue(self._ok().validate().ok)

    def test_empty_evidence_rejected(self) -> None:
        report = self._ok(evidence_ids=()).validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("evidence_ids" in err for err in report.errors),
            msg=report.errors,
        )

    def test_confidence_out_of_range_rejected(self) -> None:
        for bad in (-0.1, 1.5):
            report = self._ok(confidence=bad).validate()
            self.assertFalse(report.ok, msg=bad)
            self.assertTrue(
                any("confidence" in err for err in report.errors),
                msg=(bad, report.errors),
            )

    def test_duplicate_evidence_rejected(self) -> None:
        report = self._ok(evidence_ids=("o1", "o1")).validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("duplicate" in err for err in report.errors),
            msg=report.errors,
        )

    def test_duplicate_affected_step_rejected(self) -> None:
        report = self._ok(affected_step_ids=("s1", "s1")).validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("s1" in err for err in report.errors),
            msg=report.errors,
        )

    def test_invalid_proposed_test_aggregated_with_prefix(self) -> None:
        bad_action = SearchAction(
            kind=SearchActionKind.REPAIR_IMPLEMENTATION,
            target_branch_id="  ",  # invalid: empty branch id
            allowed_mutations=(MutationKind.LEAN_ARTIFACT,),
            rationale="bad",
        )
        report = self._ok(proposed_tests=(bad_action,)).validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any(err.startswith("proposed_tests[0]:") for err in report.errors),
            msg=report.errors,
        )

    def test_empty_hypothesis_id_rejected(self) -> None:
        report = self._ok(hypothesis_id="").validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("hypothesis_id" in err for err in report.errors),
            msg=report.errors,
        )

    def test_report_to_dict(self) -> None:
        report = FailureHypothesisReport(ok=True, errors=())
        self.assertEqual(report.to_dict(), {"ok": True, "errors": []})

    def test_huge_integer_confidence_is_reported_without_overflow(self) -> None:
        report = self._ok(confidence=10**10000).validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("out of range" in error for error in report.errors))

    def test_non_string_ids_are_reported_without_raising(self) -> None:
        hypothesis = self._ok(
            evidence_ids=(1,),  # type: ignore[arg-type]
            affected_step_ids=(2,),  # type: ignore[arg-type]
            hypothesis_id=3,  # type: ignore[arg-type]
        )

        report = hypothesis.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("hypothesis_id" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()
