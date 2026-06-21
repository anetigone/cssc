from __future__ import annotations

import unittest

from agent.proof_system.workspace import (
    ObligationGraph,
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


if __name__ == "__main__":
    unittest.main()
