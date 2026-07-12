from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import phase10_benchmark_run as runner
from scripts import phase10_benchmark_validate as validator

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "phase10_benchmark"


class Phase10ValidatorTests(unittest.TestCase):
    def test_canary_suite_passes_hardening_gate(self):
        rows = validator.load_rows(FIXTURES / "manifest.jsonl")
        self.assertEqual(validator.hardening_errors(rows, FIXTURES), [])

    def test_prompt_contamination_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            root = Path(tmp)
            (root / "bad.lean").write_text("/-- expected tactic: simp -/\ntheorem bad : True := by\n  {{proof}}\n", encoding="utf-8")
            row = {
                "task_id": "bad", "layer": "L1", "source": "bad.lean",
                "suite_version": validator.SUITE_VERSION,
                "controlled_expectation": {}, "live_expectation": {},
                "controlled_scenario": "missing.json",
            }
            errors = validator.hardening_errors([row], root)
            self.assertTrue(any("prompt contamination" in error for error in errors))

    def test_all_controlled_costs_are_labeled_simulated(self):
        for row in validator.load_rows(FIXTURES / "manifest.jsonl"):
            scenario = json.loads((FIXTURES / row["controlled_scenario"]).read_text(encoding="utf-8"))
            self.assertTrue(scenario["simulated_costs"])
            self.assertTrue(all(item["measurement"] == "simulated" for item in scenario["simulated_costs"]))


class Phase10RunnerTests(unittest.TestCase):
    def test_arm_table_freezes_controlled_and_live_arms(self):
        self.assertEqual(runner.PHASE10_ARMS["C0"], ("structured", "legacy"))
        self.assertEqual(runner.PHASE10_ARMS["A0"], ("minimal", "legacy"))
        self.assertIn("A6", runner.PHASE10_ARMS)
        self.assertEqual(runner.PHASE10_ARM_FEATURES["A5"]["model_mode"], "routed")
        self.assertEqual(runner.PHASE10_ARM_FEATURES["A6"]["model_mode"], "single_cheap")
        self.assertNotEqual(runner.PHASE10_ARM_FEATURES["C3"], runner.PHASE10_ARM_FEATURES["C4"])

    def test_unwired_controlled_arms_fail_closed(self):
        for arm in ("C2", "C3", "C4"):
            with self.subTest(arm=arm):
                rc = runner.main(["--track", "controlled", "--arm", arm])
                self.assertEqual(rc, 2)

    def test_executable_controlled_arm_reaches_replay(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            with patch(
                "scripts.phase10_benchmark_run._phase10_replay_controller",
                side_effect=AssertionError("configured builder is captured before main"),
            ):
                # C1 is not rejected by the arm-capability gate.  A missing task
                # is handled later, which distinguishes it from C2-C4.
                rc = runner.main([
                    "--track", "controlled", "--arm", "C1", "--task", "missing",
                    "--runs-root", str(Path(tmp) / "runs"),
                ])
            self.assertEqual(rc, 2)

    def test_a5_enables_phase94_routing_in_live_command(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            captured = {}

            def fake_live(*args, out: Path, **kwargs):
                captured.update(kwargs)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps({"event": "run_summary", "accepted": False, "metrics": {}, "metadata": {}}) + "\n",
                    encoding="utf-8",
                )
                return 1

            snapshot = Path(tmp) / "history.json"
            snapshot.write_text("{}", encoding="utf-8")
            with patch("scripts.phase8_benchmark_run._run_live", side_effect=fake_live):
                rc = runner.main([
                    "--task", "l1_identity", "--arm", "A5", "--repetition", "97",
                    "--runs-root", str(Path(tmp) / "runs"),
                    "--proof-model", "cheap", "--strong-proof-model", "strong",
                    "--cost-history-snapshot", str(snapshot),
                    "--overwrite",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(captured["enable_model_routing"])
            self.assertEqual(captured["strong_proof_model"], "strong")
            self.assertEqual(captured["action_cost_source"], "empirical")
            self.assertTrue(captured["remaining_budget_policy"])

    def test_a6_is_single_cheap_without_routing(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            captured = {}

            def fake_live(*args, out: Path, **kwargs):
                captured.update(kwargs)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps({"event": "run_summary", "accepted": False, "metrics": {}, "metadata": {}}) + "\n",
                    encoding="utf-8",
                )
                return 1

            snapshot = Path(tmp) / "history.json"
            snapshot.write_text("{}", encoding="utf-8")
            with patch("scripts.phase8_benchmark_run._run_live", side_effect=fake_live):
                rc = runner.main([
                    "--task", "l1_identity", "--arm", "A6", "--repetition", "98",
                    "--runs-root", str(Path(tmp) / "runs"),
                    "--proof-model", "cheap", "--cost-history-snapshot", str(snapshot), "--overwrite",
                ])
            self.assertEqual(rc, 0)
            self.assertFalse(captured["enable_model_routing"])

    def test_a2_a3_a4_map_to_distinct_runtime_configuration(self):
        expected = {
            "A2": ("static", False),
            "A3": ("empirical", False),
            "A4": ("empirical", True),
        }
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            snapshot = Path(tmp) / "history.json"
            snapshot.write_text("{}", encoding="utf-8")
            for index, (arm, pair) in enumerate(expected.items(), 120):
                captured = {}
                def fake_live(*args, out: Path, **kwargs):
                    captured.update(kwargs)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps({"event": "run_summary", "accepted": False, "metrics": {}, "metadata": {}}) + "\n", encoding="utf-8")
                    return 1
                argv = ["--task", "l1_identity", "--arm", arm, "--repetition", str(index), "--runs-root", str(Path(tmp) / "runs"), "--proof-model", "model", "--overwrite"]
                if pair[0] == "empirical":
                    argv.extend(["--cost-history-snapshot", str(snapshot)])
                with patch("scripts.phase8_benchmark_run._run_live", side_effect=fake_live):
                    self.assertEqual(runner.main(argv), 0)
                self.assertEqual((captured["action_cost_source"], captured["remaining_budget_policy"]), pair)


if __name__ == "__main__":
    unittest.main()
