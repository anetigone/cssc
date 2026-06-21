from __future__ import annotations

import unittest

from agent.proof_system.workspace import (
    ObligationGraph,
    ObligationGraphReport,
    ObligationStatus,
    ProofObligation,
    obligation_from_dict,
    obligation_graph_from_dict,
)


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


if __name__ == "__main__":
    unittest.main()
