from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent


class BaselineFixtureTests(unittest.TestCase):
    def test_fixture_files_have_stable_input_shape(self) -> None:
        seen_ids: set[str] = set()
        for name in ("fixtures_simple.json", "fixtures_complex.json"):
            path = ROOT / "data" / "tasks" / name
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertIsInstance(payload["imports"], list)
            self.assertTrue(all(isinstance(item, str) for item in payload["imports"]))
            self.assertTrue(payload["tasks"])
            for task in payload["tasks"]:
                task_id = task["task_id"]
                self.assertNotIn(task_id, seen_ids)
                seen_ids.add(task_id)
                self.assertEqual(task["proof_source"].count("{{proof}}"), 1)
                self.assertNotIn("expected_outcome", task)
                self.assertNotIn("expected_category", task)
                self.assertNotIn("expected_max_iterations", task)

    def test_complex_real_fixture_declares_real_import(self) -> None:
        path = ROOT / "data" / "tasks" / "fixtures_complex.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn("Mathlib.Data.Real.Basic", payload["imports"])


if __name__ == "__main__":
    unittest.main()
