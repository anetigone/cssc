from __future__ import annotations

import unittest

from agent.proof_system.workspace.argument import (
    ArgumentGraph,
    ArgumentStep,
    argument_graph_from_dict,
    argument_step_from_dict,
)


class ArgumentStepSerializationTest(unittest.TestCase):
    def test_round_trip_preserves_all_fields(self) -> None:
        step = ArgumentStep(
            step_id="s1",
            claim="denominator is nonzero",
            justification="strict monotonicity",
            depends_on=("s0",),
            introduced_fact_ids=("f_nonzero",),
            confidence=0.8,
        )
        restored = argument_step_from_dict(step.to_dict())
        self.assertEqual(restored, step)

    def test_optional_fields_default(self) -> None:
        step = argument_step_from_dict({"step_id": "s1", "claim": "claim"})
        self.assertEqual(step.justification, "")
        self.assertEqual(step.depends_on, ())
        self.assertIsNone(step.confidence)


class ArgumentGraphValidateTest(unittest.TestCase):
    def test_valid_linear_chain_ok(self) -> None:
        graph = ArgumentGraph(
            steps=(
                ArgumentStep(step_id="s1", claim="a", introduced_fact_ids=("fa",)),
                ArgumentStep(step_id="s2", claim="b", depends_on=("s1",)),
                ArgumentStep(step_id="s3", claim="c", depends_on=("s2",)),
            )
        )
        self.assertTrue(graph.validate().ok)

    def test_duplicate_step_id_reported(self) -> None:
        graph = ArgumentGraph(
            steps=(
                ArgumentStep(step_id="s1", claim="a"),
                ArgumentStep(step_id="s1", claim="dup"),
            )
        )
        report = graph.validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("duplicate" in e for e in report.errors))

    def test_missing_dependency_reported(self) -> None:
        graph = ArgumentGraph(
            steps=(ArgumentStep(step_id="s1", claim="a", depends_on=("ghost",)),)
        )
        report = graph.validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("missing step" in e for e in report.errors))

    def test_cycle_reported(self) -> None:
        graph = ArgumentGraph(
            steps=(
                ArgumentStep(step_id="s1", claim="a", depends_on=("s2",)),
                ArgumentStep(step_id="s2", claim="b", depends_on=("s1",)),
            )
        )
        report = graph.validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("cycle" in e for e in report.errors))

    def test_empty_graph_ok(self) -> None:
        self.assertTrue(ArgumentGraph(steps=()).validate().ok)

    def test_by_id_resolves_step(self) -> None:
        step = ArgumentStep(step_id="s1", claim="a")
        graph = ArgumentGraph(steps=(step,))
        self.assertIs(graph.by_id("s1"), step)
        self.assertIsNone(graph.by_id("missing"))


class ArgumentGraphSerializationTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        graph = ArgumentGraph(
            steps=(
                ArgumentStep(step_id="s1", claim="a", introduced_fact_ids=("fa",)),
                ArgumentStep(step_id="s2", claim="b", depends_on=("s1",)),
            )
        )
        restored = argument_graph_from_dict(graph.to_dict())
        self.assertEqual(restored, graph)


if __name__ == "__main__":
    unittest.main()
