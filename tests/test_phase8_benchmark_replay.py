"""Stage 2 tests for the Phase 8.5 controlled replay engine.

These exercise the real ``StructuredController`` driven by the scripted
``ReplayGenerator`` + ``ScenarioFakeAdapter`` (``scripts/phase8_benchmark_replay.py``)
against the 6 canary scenarios. They need no Lean toolchain and no model — the
controlled track exists precisely to prove the scheduler's causal chain without
them.

The run-script integration test at the bottom drives ``phase8_benchmark_run.main``
end-to-end with ``--track controlled`` and confirms the report script parses the
resulting trace, covering the ``_run_controlled`` rewrite.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.search.budget import BudgetConfig  # noqa: E402
from agent.tasks.task_builder import LeanTaskBuilder  # noqa: E402
import scripts.phase8.phase8_benchmark_replay as replay  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "phase8_benchmark"

# budget_profile -> (max_checks, max_model_calls), mirroring BUDGET_TABLE in the
# run script.
BUDGETS = {
    "short": (6, 4),
    "repair": (10, 8),
    "multi_obligation": (20, 16),
}

# task_id -> expected (accepted, workspace_status, min accepted, [open ids])
# derived from manifest.jsonl + scenario semantics.
EXPECTATIONS = {
    "l1_canary_true": dict(accepted=True, status="accepted", acc=1, open=0),
    "l2_canary_nat_omega": dict(accepted=True, status="accepted", acc=1, open=0),
    "l3_canary_capability_gap": dict(accepted=False, status="blocked", acc=0, open=0),
    "l4_canary_two_helpers": dict(accepted=True, status="accepted", acc=3, open=0),
    "l5_canary_rep_change": dict(accepted=True, status="accepted", acc=1, open=0),
    "l6_canary_partial": dict(accepted=False, status="partial", acc=1, open=2),
}

MANIFEST = {
    row["task_id"]: row
    for row in (
        json.loads(line)
        for line in (FIXTURES / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
}


def _run(task_id: str, frontier_policy: str = "legacy"):
    row = MANIFEST[task_id]
    scenario = json.loads(
        (FIXTURES / "scenarios" / f"{task_id}.json").read_text(encoding="utf-8")
    )
    source = (FIXTURES / f"{task_id}.lean").read_text(encoding="utf-8")
    tasks = LeanTaskBuilder().build_from_source(source, source_path=f"{task_id}.lean")
    assert len(tasks) == 1, f"{task_id}: expected 1 task, got {len(tasks)}"
    max_checks, max_model_calls = BUDGETS[row["budget_profile"]]
    with tempfile.TemporaryDirectory() as tmp:
        controller, _generator, _adapter = replay.build_replay_controller(
            scenario=scenario,
            frontier_policy=frontier_policy,
            budget_config=BudgetConfig(
                max_checks=max_checks, max_model_calls=max_model_calls
            ),
            workspace_root=tmp,
        )
        result = controller.run(tasks[0])
    return result


class ControlledReplayTests(unittest.TestCase):
    def test_l1_controlled_accepted(self) -> None:
        result = _run("l1_canary_true")
        self.assertTrue(result.accepted)
        self.assertEqual(result.stop_reason, "accepted")
        summary = result.metadata["result_summary"]
        self.assertEqual(summary["workspace_status"], "accepted")
        self.assertEqual(len(summary["accepted_obligations"]), 1)

    def test_l2_controlled_repair_sequence(self) -> None:
        result = _run("l2_canary_nat_omega")
        self.assertTrue(result.accepted)
        self.assertEqual(len(result.attempts), 3)
        # The sequence is rfl (fail) -> simp [h] (fail) -> omega (accept).
        texts = [attempt.edit.text for attempt in result.attempts]
        self.assertEqual(texts, ["rfl", "simp [h]", "omega"])
        self.assertTrue(result.attempts[-1].check_result.accepted)

    def test_l3_controlled_blocked(self) -> None:
        result = _run("l3_canary_capability_gap")
        self.assertFalse(result.accepted)
        summary = result.metadata["result_summary"]
        self.assertEqual(summary["workspace_status"], "blocked")
        self.assertEqual(len(summary["blocked_obligations"]), 1)
        # A capability-test attempt was recorded with the expected category.
        cap = [
            a for a in result.attempts
            if a.edit.action == "capability_test"
        ]
        self.assertTrue(cap, "expected a capability_test attempt")
        self.assertEqual(
            cap[0].check_result.category.value, "unknown_identifier"
        )

    def test_l4_controlled_decompose_accepted(self) -> None:
        result = _run("l4_canary_two_helpers")
        self.assertTrue(result.accepted)
        summary = result.metadata["result_summary"]
        self.assertEqual(summary["workspace_status"], "accepted")
        # root + helper1 + helper2 all accepted.
        self.assertEqual(len(summary["accepted_obligations"]), 3)
        self.assertTrue(result.metadata.get("decompose_records"))

    def test_l5_controlled_representation_change(self) -> None:
        result = _run("l5_canary_rep_change")
        self.assertTrue(result.accepted)
        # representation change path was exercised (manifest required_action_kind).
        self.assertTrue(result.metadata.get("representation_records"))

    def test_l6_controlled_partial(self) -> None:
        result = _run("l6_canary_partial")
        self.assertFalse(result.accepted)
        summary = result.metadata["result_summary"]
        self.assertEqual(summary["workspace_status"], "partial")
        # helper1 accepted; helper2 + root left open.
        accepted_ids = {
            obl["obligation_id"] for obl in summary["accepted_obligations"]
        }
        self.assertIn("helper1", accepted_ids)
        self.assertGreaterEqual(len(summary["open_obligations"]), 2)

    def test_controlled_records_zero_tokens_and_nonzero_calls(self) -> None:
        # The controlled track isolates scheduling cost from token cost: a
        # scripted generator emits no token usage, so tokens are zero while
        # model_calls counts frontier pops.
        result = _run("l4_canary_two_helpers")
        self.assertEqual(result.metrics.model_input_tokens, 0)
        self.assertEqual(result.metrics.model_output_tokens, 0)
        self.assertGreaterEqual(result.metrics.budget_model_calls_used, 1)

    def test_frontier_policy_recorded_for_all_structured_arms(self) -> None:
        for policy in (
            "legacy",
            "cost_aware_v1",
            "cost_aware_v2",
            "value_per_cost_v1",
        ):
            with self.subTest(policy=policy):
                result = _run("l1_canary_true", frontier_policy=policy)
                self.assertEqual(result.metadata["frontier_policy"], policy)

    def test_all_canaries_match_expectations(self) -> None:
        # Cross-cutting table check: every canary reaches its manifest terminal.
        for task_id, expected in EXPECTATIONS.items():
            with self.subTest(task_id=task_id):
                result = _run(task_id)
                summary = result.metadata["result_summary"]
                self.assertEqual(
                    result.accepted, expected["accepted"], msg=task_id
                )
                self.assertEqual(
                    summary["workspace_status"], expected["status"], msg=task_id
                )
                self.assertEqual(
                    len(summary["accepted_obligations"]),
                    expected["acc"],
                    msg=task_id,
                )


class RunScriptControlledTests(unittest.TestCase):
    """End-to-end: phase8_benchmark_run --track controlled writes a parseable trace."""

    def test_controlled_run_writes_trace_and_provenance(self) -> None:
        import scripts.phase8.phase8_benchmark_run as run
        import scripts.phase8.phase8_benchmark_report as rep

        with tempfile.TemporaryDirectory() as runs_root:
            argv = [
                "--track", "controlled",
                "--task", "l4_canary_two_helpers",
                "--arm", "A1",
                "--suite-version", "test-controlled",
                "--runs-root", str(Path(runs_root) / "phase8"),
            ]
            rc = run.main(argv)
            self.assertEqual(rc, 0, "controlled run should succeed")

            trace = (
                Path(runs_root)
                / "phase8"
                / "test-controlled"
                / "A1"
                / "l4_canary_two_helpers"
                / "1.jsonl"
            )
            meta = trace.with_suffix(".meta.json")
            self.assertTrue(trace.is_file(), f"trace missing: {trace}")
            self.assertTrue(meta.is_file(), f"provenance missing: {meta}")

            provenance = json.loads(meta.read_text(encoding="utf-8"))
            self.assertEqual(provenance["track"], "controlled")
            self.assertEqual(provenance["status"], "completed")
            self.assertIsNone(provenance["proof_model"])

            # The report script must parse the controlled trace unchanged.
            rc_report = rep.main(
                [
                    "--runs-dir", str(Path(runs_root) / "phase8"),
                    "--suite-version", "test-controlled",
                    "--output", str(Path(runs_root) / "report.md"),
                ]
            )
            self.assertEqual(rc_report, 0, "report should parse controlled trace")
            report = (Path(runs_root) / "report.md").read_text(encoding="utf-8")
            self.assertIn("l4_canary_two_helpers", report)
            self.assertIn("structured", report)
            self.assertIn("legacy", report)

    def test_controlled_rejects_minimal_arm(self) -> None:
        import scripts.phase8.phase8_benchmark_run as run

        with tempfile.TemporaryDirectory() as runs_root:
            argv = [
                "--track", "controlled",
                "--task", "l1_canary_true",
                "--arm", "A0",
                "--suite-version", "test-controlled-a0",
                "--runs-root", str(Path(runs_root) / "phase8"),
            ]
            rc = run.main(argv)
            self.assertNotEqual(rc, 0, "controlled + A0 should fail")

    def test_controlled_collision_protection(self) -> None:
        import scripts.phase8.phase8_benchmark_run as run

        with tempfile.TemporaryDirectory() as runs_root:
            common = [
                "--track", "controlled",
                "--task", "l1_canary_true",
                "--arm", "A1",
                "--suite-version", "test-controlled-coll",
                "--runs-root", str(Path(runs_root) / "phase8"),
            ]
            self.assertEqual(run.main(common), 0)
            # Same tuple without --overwrite must be refused.
            self.assertNotEqual(run.main(common), 0)
            # --overwrite succeeds.
            self.assertEqual(run.main(common + ["--overwrite"]), 0)


if __name__ == "__main__":
    unittest.main()
