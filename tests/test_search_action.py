from __future__ import annotations

import unittest

from agent.proof_system.workspace.action import (
    DEFAULT_ALLOWED_MUTATIONS,
    MutationKind,
    SearchAction,
    SearchActionKind,
    SearchActionReport,
    search_action_from_dict,
)


class SearchActionSerializationTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.REPAIR_IMPLEMENTATION,
            target_branch_id="b1",
            target_step_ids=("s3",),
            allowed_mutations=(MutationKind.LEAN_ARTIFACT,),
            rationale="fix tactic typo in helper proof",
        )
        restored = search_action_from_dict(action.to_dict())
        self.assertEqual(restored, action)

    def test_round_trip_empty_collections(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.RUN_CHECK,
            target_branch_id="b1",
            rationale="re-run checker after edit",
        )
        self.assertEqual(action.allowed_mutations, ())
        self.assertEqual(action.target_step_ids, ())
        restored = search_action_from_dict(action.to_dict())
        self.assertEqual(restored, action)


class SearchActionValidateTest(unittest.TestCase):
    def test_each_kind_valid_with_default_scope(self) -> None:
        # Every kind is valid when it carries exactly its default scope.
        for kind in SearchActionKind:
            action = SearchAction(
                kind=kind,
                target_branch_id="b1",
                allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[kind],
                rationale="probe",
            )
            report = action.validate()
            self.assertTrue(
                report.ok,
                msg=f"{kind}: {report.errors}",
            )

    def test_narrowing_scope_is_allowed(self) -> None:
        # REPAIR_IMPLEMENTATION may narrow to LEAN_ARTIFACT only.
        action = SearchAction(
            kind=SearchActionKind.REPAIR_IMPLEMENTATION,
            target_branch_id="b1",
            allowed_mutations=(MutationKind.LEAN_ARTIFACT,),
            rationale="touch only the Lean artifact",
        )
        self.assertTrue(action.validate().ok)

    def test_broadening_scope_is_rejected(self) -> None:
        # REPAIR_IMPLEMENTATION may not silently touch the argument step.
        action = SearchAction(
            kind=SearchActionKind.REPAIR_IMPLEMENTATION,
            target_branch_id="b1",
            allowed_mutations=(
                MutationKind.LEAN_ARTIFACT,
                MutationKind.ARGUMENT_STEP,
            ),
            rationale="repair",
        )
        report = action.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("argument_step" in err for err in report.errors),
            msg=report.errors,
        )

    def test_empty_branch_id_rejected(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.IMPLEMENT,
            target_branch_id="  ",
            allowed_mutations=(MutationKind.LEAN_ARTIFACT,),
            rationale="implement",
        )
        report = action.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("target_branch_id" in err for err in report.errors),
            msg=report.errors,
        )

    def test_empty_rationale_rejected(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.IMPLEMENT,
            target_branch_id="b1",
            allowed_mutations=(MutationKind.LEAN_ARTIFACT,),
            rationale="",
        )
        report = action.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("rationale" in err for err in report.errors),
            msg=report.errors,
        )

    def test_duplicate_step_id_rejected(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.REFINE_ARGUMENT,
            target_branch_id="b1",
            target_step_ids=("s1", "s1"),
            allowed_mutations=(MutationKind.ARGUMENT_STEP,),
            rationale="refine",
        )
        report = action.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("s1" in err for err in report.errors),
            msg=report.errors,
        )

    def test_duplicate_allowed_mutation_rejected(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.CHANGE_REPRESENTATION,
            target_branch_id="b1",
            allowed_mutations=(
                MutationKind.LEAN_ARTIFACT,
                MutationKind.LEAN_ARTIFACT,
            ),
            rationale="switch representation",
        )
        report = action.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("duplicate" in err for err in report.errors),
            msg=report.errors,
        )

    def test_empty_step_id_rejected(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.PROPOSE_ARGUMENT,
            target_branch_id="b1",
            target_step_ids=("",),
            allowed_mutations=(MutationKind.ARGUMENT_STEP,),
            rationale="propose",
        )
        report = action.validate()
        self.assertFalse(report.ok)
        self.assertTrue(
            any("empty" in err for err in report.errors),
            msg=report.errors,
        )

    def test_report_to_dict(self) -> None:
        report = SearchActionReport(ok=False, errors=("a", "b"))
        self.assertEqual(
            report.to_dict(), {"ok": False, "errors": ["a", "b"]}
        )

    def test_default_scope_table_is_complete(self) -> None:
        # Every action kind has an entry; guard against adding a kind and
        # forgetting its default scope.
        self.assertEqual(
            set(DEFAULT_ALLOWED_MUTATIONS), set(SearchActionKind)
        )


if __name__ == "__main__":
    unittest.main()
