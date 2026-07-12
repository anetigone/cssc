from __future__ import annotations

import unittest

from agent.proof_system.base import CheckResult, DiagnosticCategory
from agent.proof_system.workspace import (
    FormalSpecification,
    ObligationGraph,
    ObligationGraphReport,
    ObligationStatus,
    ProofBranch,
    ProofObligation,
    ProofWorkspace,
    WorkspaceStatus,
    initialize_from_task,
    obligation_from_dict,
    obligation_graph_from_dict,
    workspace_from_dict,
)
from agent.tasks.types import ProofTask


def _obligation(
    *,
    obligation_id: str = "root",
    version: int = 1,
    title: str = "root theorem",
    dependency_ids: tuple[str, ...] = (),
    status: ObligationStatus = ObligationStatus.OPEN,
    lean_statement: str = "theorem root : True := by\n  trivial",
) -> ProofObligation:
    return ProofObligation(
        obligation_id=obligation_id,
        version=version,
        title=title,
        lean_statement=lean_statement,
        dependency_ids=dependency_ids,
        status=status,
    )


def _check_result(*, accepted: bool = True) -> CheckResult:
    return CheckResult(
        accepted=accepted,
        category=(
            DiagnosticCategory.PROOF_ACCEPTED
            if accepted
            else DiagnosticCategory.UNSOLVED_GOALS
        ),
        raw_output="",
    )


class ProofObligationTests(unittest.TestCase):
    def test_defaults(self) -> None:
        obligation = ProofObligation(obligation_id="root", version=1)
        self.assertEqual(obligation.status, ObligationStatus.OPEN)
        self.assertEqual(obligation.assumptions, ())
        self.assertEqual(obligation.dependency_ids, ())
        self.assertEqual(obligation.title, "")

    def test_round_trip(self) -> None:
        obligation = _obligation(
            obligation_id="root",
            version=1,
            dependency_ids=("helper",),
            status=ObligationStatus.ACCEPTED,
        )
        restored = obligation_from_dict(obligation.to_dict())

        self.assertEqual(restored, obligation)
        # Enum survives the dict round-trip as its value, then restores.
        self.assertEqual(restored.status, ObligationStatus.ACCEPTED)


class ObligationGraphTests(unittest.TestCase):
    def test_root_and_active_lookup(self) -> None:
        root = _obligation(obligation_id="root", status=ObligationStatus.OPEN)
        helper = _obligation(
            obligation_id="helper",
            dependency_ids=("root",),
            status=ObligationStatus.SUPERSEDED,
        )
        graph = ObligationGraph(
            obligations=(root, helper),
            root_obligation_id="root",
        )

        self.assertIs(graph.root(), root)
        self.assertIs(graph.by_id("helper"), helper)
        self.assertIsNone(graph.by_id("missing"))
        self.assertEqual(graph.active(), (root,))
        self.assertEqual(graph.superseded(), (helper,))

    def test_with_obligation_replaces_only_exact_version(self) -> None:
        root = _obligation(obligation_id="root", version=1)
        graph = ObligationGraph(obligations=(root,), root_obligation_id="root")

        accepted_root = _obligation(
            obligation_id="root", version=1, status=ObligationStatus.ACCEPTED
        )
        updated = graph.with_obligation(accepted_root)

        self.assertEqual(len(updated.obligations), 1)
        self.assertIs(updated.by_id("root"), accepted_root)

    def test_round_trip(self) -> None:
        root = _obligation(obligation_id="root")
        helper = _obligation(obligation_id="helper", dependency_ids=("root",))
        graph = ObligationGraph(
            obligations=(root, helper),
            root_obligation_id="root",
        )

        restored = obligation_graph_from_dict(graph.to_dict())

        self.assertEqual(restored.root_obligation_id, "root")
        self.assertEqual(
            [o.obligation_id for o in restored.obligations], ["root", "helper"]
        )
        self.assertEqual(restored.by_id("helper").dependency_ids, ("root",))


def _valid_graph() -> ObligationGraph:
    root = _obligation(obligation_id="root", dependency_ids=("helper",))
    helper = _obligation(obligation_id="helper")
    return ObligationGraph(
        obligations=(root, helper),
        root_obligation_id="root",
    )


class ObligationGraphValidationTests(unittest.TestCase):
    def test_valid_graph_passes(self) -> None:
        report = _valid_graph().validate()

        self.assertTrue(report.ok)
        self.assertEqual(report.errors, ())
        self.assertEqual(report.to_dict(), {"ok": True, "errors": []})

    def test_missing_root_reported(self) -> None:
        root = _obligation(obligation_id="root")
        graph = ObligationGraph(obligations=(root,), root_obligation_id="missing")

        report = graph.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("missing" in e for e in report.errors))

    def test_missing_dependency_reported(self) -> None:
        root = _obligation(obligation_id="root", dependency_ids=("ghost",))
        graph = ObligationGraph(
            obligations=(root,),
            root_obligation_id="root",
        )

        report = graph.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("ghost" in e for e in report.errors))

    def test_cycle_reported(self) -> None:
        a = _obligation(obligation_id="a", dependency_ids=("b",))
        b = _obligation(obligation_id="b", dependency_ids=("a",))
        graph = ObligationGraph(obligations=(a, b), root_obligation_id="a")

        report = graph.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("cycle" in e for e in report.errors))

    def test_unreachable_from_root_reported(self) -> None:
        root = _obligation(obligation_id="root", dependency_ids=("helper",))
        helper = _obligation(obligation_id="helper")
        # An island outside the root's proof-dependency closure.
        island = _obligation(obligation_id="island")
        graph = ObligationGraph(
            obligations=(root, helper, island),
            root_obligation_id="root",
        )

        report = graph.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("island" in e for e in report.errors))


class ObligationGraphVersioningTests(unittest.TestCase):
    def test_new_version_supersedes_previous(self) -> None:
        graph = ObligationGraph(
            obligations=(_obligation(obligation_id="root", version=1),),
            root_obligation_id="root",
        )

        updated = graph.new_version(
            "root", lean_statement="theorem root : True := by\n  decide"
        )

        # Both versions retained; latest wins the id slot.
        self.assertEqual(len(updated.obligations), 2)
        latest = updated.by_id("root")
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.status, ObligationStatus.OPEN)
        self.assertEqual(
            updated.superseded()[0].status, ObligationStatus.SUPERSEDED
        )

    def test_new_version_active_set_still_valid(self) -> None:
        graph = _valid_graph()
        updated = graph.new_version("helper")

        report = updated.validate()

        self.assertTrue(
            report.ok, f"graph invalid after new_version: {report.errors}"
        )

    def test_repeated_versions_preserve_full_history(self) -> None:
        graph = ObligationGraph(
            obligations=(_obligation(obligation_id="root", version=1),),
            root_obligation_id="root",
        )

        updated = graph.new_version("root").new_version("root")

        self.assertEqual(
            [(o.version, o.status) for o in updated.obligations],
            [
                (1, ObligationStatus.SUPERSEDED),
                (2, ObligationStatus.SUPERSEDED),
                (3, ObligationStatus.OPEN),
            ],
        )

    def test_multiple_active_versions_are_invalid(self) -> None:
        graph = ObligationGraph(
            obligations=(
                _obligation(obligation_id="root", version=1),
                _obligation(obligation_id="root", version=2),
            ),
            root_obligation_id="root",
        )

        report = graph.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("active versions" in error for error in report.errors))

    def test_active_dependency_on_superseded_reported(self) -> None:
        root = _obligation(obligation_id="root", version=1)
        helper = _obligation(obligation_id="helper", dependency_ids=("root",))
        graph = ObligationGraph(
            obligations=(root, helper),
            root_obligation_id="root",
        )
        # Revise the root; the helper still points at the (now superseded) id
        # slot, but by_id resolves to the newest version, so the helper's edge
        # is fine. To force the failure, point at an id whose only version is
        # superseded by constructing it manually.
        superseded_only = ProofObligation(
            obligation_id="ghost",
            version=1,
            status=ObligationStatus.SUPERSEDED,
        )
        bad_helper = _obligation(obligation_id="bad", dependency_ids=("ghost",))
        bad_graph = ObligationGraph(
            obligations=(root, bad_helper, superseded_only),
            root_obligation_id="root",
        )

        report = bad_graph.validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("superseded" in e for e in report.errors))


class ProofWorkspaceTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        root = _obligation(obligation_id="root")
        graph = ObligationGraph(obligations=(root,), root_obligation_id="root")
        workspace = ProofWorkspace(
            workspace_id="run-1",
            specification=FormalSpecification(statement_nl="show True"),
            obligation_graph=graph,
            root_obligation_ids=("root",),
            status=WorkspaceStatus.SEARCHING,
        )

        restored = workspace_from_dict(workspace.to_dict())

        self.assertEqual(restored.workspace_id, "run-1")
        self.assertEqual(restored.version, 1)
        self.assertEqual(restored.status, WorkspaceStatus.SEARCHING)
        self.assertEqual(restored.root_obligation_ids, ("root",))
        self.assertEqual(restored.obligation_graph.root().obligation_id, "root")
        self.assertEqual(restored.specification.statement_nl, "show True")

    def test_round_trip_preserves_partial_status(self) -> None:
        # PARTIAL is a first-class terminal status and must survive
        # serialization (the run finalizer sets it; the trace reloads it).
        root = _obligation(obligation_id="root")
        graph = ObligationGraph(obligations=(root,), root_obligation_id="root")
        workspace = ProofWorkspace(
            workspace_id="run-1",
            obligation_graph=graph,
            root_obligation_ids=("root",),
            status=WorkspaceStatus.PARTIAL,
        )
        restored = workspace_from_dict(workspace.to_dict())
        self.assertEqual(restored.status, WorkspaceStatus.PARTIAL)

    def test_round_trip_preserves_branches(self) -> None:
        root = _obligation(obligation_id="root")
        graph = ObligationGraph(obligations=(root,), root_obligation_id="root")
        branch = ProofBranch(
            branch_id="b1",
            obligation_id="root",
            obligation_version=1,
        )
        workspace = ProofWorkspace(
            workspace_id="run-1",
            specification=FormalSpecification(statement_nl="show True"),
            obligation_graph=graph,
            branches=(branch,),
            root_obligation_ids=("root",),
            status=WorkspaceStatus.SEARCHING,
        )

        restored = workspace_from_dict(workspace.to_dict())

        self.assertEqual(restored.branches, (branch,))
        # Default-constructed workspaces carry no branches.
        seeded = initialize_from_task(
            ProofTask(
                task_id="demo",
                source_template="theorem demo : True := by\n  {{proof}}\n",
            )
        )
        self.assertEqual(seeded.branches, ())
        self.assertEqual(workspace_from_dict(seeded.to_dict()).branches, ())

    def test_initialize_from_task_seeds_single_root(self) -> None:
        task = ProofTask(
            task_id="demo",
            source_template="theorem demo : True := by\n  {{proof}}\n",
            metadata={"natural_language_problem": "prove True"},
        )

        workspace = initialize_from_task(task)

        self.assertEqual(workspace.workspace_id, "demo")
        self.assertEqual(workspace.version, 1)
        self.assertIsNone(workspace.parent_version)
        self.assertEqual(workspace.root_obligation_ids, ("demo",))
        self.assertEqual(workspace.status, WorkspaceStatus.SEARCHING)
        root = workspace.obligation_graph.root()
        self.assertIsNotNone(root)
        assert root is not None
        self.assertEqual(root.version, 1)
        self.assertEqual(root.statement_nl, "prove True")
        self.assertEqual(root.lean_statement, task.source_template)
        # The freshly seeded graph must satisfy the DAG invariant.
        self.assertTrue(workspace.obligation_graph.validate().ok)


class ProofWorkspaceValidationTests(unittest.TestCase):
    def _workspace(self, *branches: ProofBranch) -> ProofWorkspace:
        root = _obligation(obligation_id="root")
        return ProofWorkspace(
            workspace_id="run-1",
            obligation_graph=ObligationGraph(
                obligations=(root,), root_obligation_id="root"
            ),
            branches=branches,
            root_obligation_ids=("root",),
            status=WorkspaceStatus.SEARCHING,
        )

    def test_valid_parent_child_branch_tree(self) -> None:
        parent = ProofBranch("b1", "root", 1)
        child = ProofBranch("b2", "root", 1, parent_branch_id="b1")

        report = self._workspace(parent, child).validate()

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.to_dict(), {"ok": True, "errors": []})

    def test_duplicate_and_missing_parent_are_reported(self) -> None:
        first = ProofBranch("b1", "root", 1)
        duplicate = ProofBranch("b1", "root", 1, parent_branch_id="ghost")

        report = self._workspace(first, duplicate).validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("duplicate proof branch" in e for e in report.errors))
        self.assertTrue(any("missing parent" in e for e in report.errors))

    def test_parent_cycle_is_reported(self) -> None:
        first = ProofBranch("b1", "root", 1, parent_branch_id="b2")
        second = ProofBranch("b2", "root", 1, parent_branch_id="b1")

        report = self._workspace(first, second).validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("parent cycle" in e for e in report.errors))

    def test_missing_obligation_version_is_reported(self) -> None:
        branch = ProofBranch("b1", "root", 2)

        report = self._workspace(branch).validate()

        self.assertFalse(report.ok)
        self.assertTrue(any("missing obligation" in e for e in report.errors))


class WorkspaceMutationTests(unittest.TestCase):
    def _workspace(self) -> ProofWorkspace:
        task = ProofTask(
            task_id="root",
            source_template="theorem root : True := by\n  {{proof}}\n",
        )
        return initialize_from_task(task)

    def test_decompose_adds_children_and_bumps_version(self) -> None:
        workspace = self._workspace()
        child = ProofObligation(
            obligation_id="helper",
            version=1,
            lean_statement="lemma helper : True := by\n  trivial",
        )

        updated = workspace.decompose("root", (child,))

        self.assertEqual(updated.version, workspace.version + 1)
        self.assertEqual(updated.parent_version, workspace.version)
        self.assertIsNotNone(updated.obligation_graph.by_id("helper"))
        root = updated.obligation_graph.by_id("root")
        self.assertEqual(root.version, 2)
        self.assertEqual(root.dependency_ids, ("helper",))
        self.assertEqual(
            [o.version for o in updated.obligation_graph.superseded()], [1]
        )
        # Decomposition must keep the DAG invariant intact.
        self.assertTrue(
            updated.obligation_graph.validate().ok,
            f"invalid after decompose: {updated.obligation_graph.validate().errors}",
        )

    def test_decompose_unknown_obligation_raises(self) -> None:
        workspace = self._workspace()
        with self.assertRaises(KeyError):
            workspace.decompose("ghost", ())

    def test_register_accepted_fact_marks_obligation_and_records_provenance(
        self,
    ) -> None:
        workspace = self._workspace()

        updated = workspace.register_accepted_fact(
            "root",
            statement="root proven",
            source_attempt_index=3,
            check_result=_check_result(),
            safety_accepted=True,
        )

        self.assertEqual(updated.version, workspace.version + 1)
        obligation = updated.obligation_graph.by_id("root")
        self.assertEqual(obligation.status, ObligationStatus.ACCEPTED)
        self.assertEqual(len(updated.accepted_facts), 1)
        fact = updated.accepted_facts[0]
        self.assertEqual(fact.obligation_id, "root")
        self.assertEqual(fact.obligation_version, 1)
        self.assertEqual(fact.source_attempt_index, 3)
        self.assertEqual(fact.checker_category, "proof_accepted")
        self.assertTrue(fact.safety_accepted)

    def test_register_accepted_fact_refuses_superseded_obligation(self) -> None:
        workspace = self._workspace()
        # Revise the root so its v1 becomes superseded; by_id resolves to v2.
        revised_graph = workspace.obligation_graph.new_version("root")
        from dataclasses import replace

        revised_workspace = replace(workspace, obligation_graph=revised_graph)

        # Force a superseded-only entry by registering against an id we then
        # make dead: build a graph whose sole version of an id is superseded.
        superseded_only = ProofObligation(
            obligation_id="dead",
            version=1,
            status=ObligationStatus.SUPERSEDED,
        )
        bad_graph = ObligationGraph(
            obligations=(
                workspace.obligation_graph.root(),
                superseded_only,
            ),
            root_obligation_id="root",
        )
        bad_workspace = replace(workspace, obligation_graph=bad_graph)

        with self.assertRaises(ValueError):
            bad_workspace.register_accepted_fact(
                "dead",
                statement="stale",
                source_attempt_index=1,
                check_result=_check_result(),
                safety_accepted=True,
            )

        # Sanity: registering against a live obligation still works.
        self.assertTrue(
            revised_workspace.register_accepted_fact(
                "root",
                statement="ok",
                source_attempt_index=1,
                check_result=_check_result(),
                safety_accepted=True,
            ).obligation_graph.validate().ok
        )

    def test_register_accepted_fact_requires_checker_and_safety_acceptance(self) -> None:
        workspace = self._workspace()

        with self.assertRaisesRegex(ValueError, "checker"):
            workspace.register_accepted_fact(
                "root",
                statement="not proven",
                source_attempt_index=1,
                check_result=_check_result(accepted=False),
                safety_accepted=True,
            )
        with self.assertRaisesRegex(ValueError, "safety"):
            workspace.register_accepted_fact(
                "root",
                statement="unsafe",
                source_attempt_index=1,
                check_result=_check_result(),
                safety_accepted=False,
            )
        inconsistent = CheckResult(
            accepted=True,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="",
        )
        with self.assertRaisesRegex(ValueError, "proof_accepted"):
            workspace.register_accepted_fact(
                "root",
                statement="inconsistent",
                source_attempt_index=1,
                check_result=inconsistent,
                safety_accepted=True,
            )

    def test_accepting_latest_version_preserves_superseded_history(self) -> None:
        workspace = self._workspace()
        graph = workspace.obligation_graph.new_version("root").new_version("root")
        from dataclasses import replace

        workspace = replace(workspace, obligation_graph=graph)
        updated = workspace.register_accepted_fact(
            "root",
            statement="root proven",
            source_attempt_index=3,
            check_result=_check_result(),
            safety_accepted=True,
        )

        self.assertEqual(
            [(o.version, o.status) for o in updated.obligation_graph.obligations],
            [
                (1, ObligationStatus.SUPERSEDED),
                (2, ObligationStatus.SUPERSEDED),
                (3, ObligationStatus.ACCEPTED),
            ],
        )


if __name__ == "__main__":
    unittest.main()
