from __future__ import annotations

import unittest

from agent.proof_system.workspace import (
    FormalSpecification,
    ObligationGraph,
    ObligationGraphReport,
    ObligationStatus,
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

    def test_with_obligation_replaces_prior_version(self) -> None:
        root = _obligation(obligation_id="root", version=1)
        graph = ObligationGraph(obligations=(root,), root_obligation_id="root")

        root_v2 = _obligation(obligation_id="root", version=2)
        updated = graph.with_obligation(root_v2)

        self.assertEqual(len(updated.obligations), 1)
        self.assertIs(updated.by_id("root"), root_v2)

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
    root = _obligation(obligation_id="root")
    helper = _obligation(obligation_id="helper", dependency_ids=("root",))
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
        helper = _obligation(obligation_id="helper", dependency_ids=("ghost",))
        graph = ObligationGraph(
            obligations=(_obligation(obligation_id="root"), helper),
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
        root = _obligation(obligation_id="root")
        orphan = _obligation(obligation_id="orphan", dependency_ids=("root",))
        # An island that neither depends on root nor is depended on by root.
        island = _obligation(obligation_id="island")
        graph = ObligationGraph(
            obligations=(root, orphan, island),
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
            dependency_ids=("root",),
            lean_statement="lemma helper : True := by\n  trivial",
        )

        updated = workspace.decompose("root", (child,))

        self.assertEqual(updated.version, workspace.version + 1)
        self.assertEqual(updated.parent_version, workspace.version)
        self.assertIsNotNone(updated.obligation_graph.by_id("helper"))
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
            "root", statement="root proven", source_attempt_index=3
        )

        self.assertEqual(updated.version, workspace.version + 1)
        obligation = updated.obligation_graph.by_id("root")
        self.assertEqual(obligation.status, ObligationStatus.ACCEPTED)
        self.assertEqual(len(updated.accepted_facts), 1)
        fact = updated.accepted_facts[0]
        self.assertEqual(fact.obligation_id, "root")
        self.assertEqual(fact.obligation_version, 1)
        self.assertEqual(fact.source_attempt_index, 3)

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
            bad_workspace.register_accepted_fact("dead", statement="stale")

        # Sanity: registering against a live obligation still works.
        self.assertTrue(
            revised_workspace.register_accepted_fact(
                "root", statement="ok"
            ).obligation_graph.validate().ok
        )


if __name__ == "__main__":
    unittest.main()
