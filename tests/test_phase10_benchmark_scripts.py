from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(runner.base.ARM_TABLE["C0"], ("structured", "legacy"))
        self.assertEqual(runner.base.ARM_TABLE["A0"], ("minimal", "legacy"))
        self.assertIn("A6", runner.base.ARM_TABLE)


if __name__ == "__main__":
    unittest.main()
