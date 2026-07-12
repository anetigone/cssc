from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.proof_system.base import CandidateEdit
from agent.tasks.task_builder import (
    LeanTaskBuilder,
    TaskBuildError,
    TaskBuilderConfig,
    materialize_task_dependencies,
)


class LeanTaskBuilderTests(unittest.TestCase):
    def test_builds_task_from_explicit_marker(self) -> None:
        builder = LeanTaskBuilder()
        source = "import Mathlib\n\ntheorem sample : True := by\n  {{proof}}\n"

        tasks = builder.build_from_source(source, source_path="Basic.lean", split="test")

        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.hole_marker, "{{proof}}")
        self.assertIn("{{proof}}", task.source_template)
        self.assertEqual(task.metadata["split"], "test")
        self.assertEqual(task.metadata["source_imports"], ("Mathlib",))
        self.assertEqual(task.metadata["hole_kind"], "marker")
        self.assertEqual(task.task_id, "Basic")
        self.assertEqual(task.metadata["task_name"], "Basic")
        self.assertEqual(task.metadata["hole_id"], task.task_id)

    def test_rejects_multiple_sorries_by_default(self) -> None:
        builder = LeanTaskBuilder()
        source = (
            "theorem one : True := by\n"
            "  sorry\n\n"
            "theorem two : True := by\n"
            "  sorry\n"
        )

        with self.assertRaises(TaskBuildError):
            builder.build_from_source(source, task_id_prefix="basic")

    def test_can_opt_into_one_task_per_standalone_sorry(self) -> None:
        builder = LeanTaskBuilder(TaskBuilderConfig(allow_multiple_sorry_tasks=True))
        source = (
            "theorem one : True := by\n"
            "  sorry\n\n"
            "theorem two : True := by\n"
            "  sorry\n"
        )

        tasks = builder.build_from_source(source, task_id_prefix="basic")

        self.assertEqual(len(tasks), 2)
        self.assertEqual([task.metadata["task_name"] for task in tasks], ["basic.one", "basic.two"])
        self.assertEqual([task.task_id for task in tasks], ["basic.one", "basic.two"])
        self.assertEqual(tasks[0].metadata["hole_line"], 2)
        self.assertEqual(tasks[1].metadata["hole_line"], 5)
        self.assertIn("{{proof}}", tasks[0].source_template)
        self.assertEqual(tasks[0].source_template.count("{{proof}}"), 1)
        self.assertEqual(tasks[0].source_template.count("sorry"), 0)
        self.assertNotIn("theorem two", tasks[0].source_template)
        self.assertEqual(tasks[0].metadata["active_hole_count"], 1)
        self.assertEqual(tasks[0].metadata["source_hole_count"], 2)
        self.assertFalse(tasks[0].metadata["has_inactive_holes"])
        self.assertEqual(tasks[0].metadata["dependency_task_ids"], ())
        self.assertEqual(tasks[1].metadata["dependency_task_ids"], ("basic.one",))
        self.assertNotIn("sorry", tasks[1].source_template)
        self.assertIn("{{dependency:basic.one}}", tasks[1].source_template)

        with self.assertRaises(TaskBuildError):
            materialize_task_dependencies(tasks[1], {})
        materialized = materialize_task_dependencies(
            tasks[1], {"basic.one": "trivial"}
        )
        self.assertNotIn("{{dependency:", materialized.source_template)
        self.assertEqual(materialized.source_template.count("{{proof}}"), 1)
        self.assertIn("theorem one : True := by\n  trivial", materialized.source_template)

    def test_can_opt_into_one_task_per_explicit_marker(self) -> None:
        builder = LeanTaskBuilder(TaskBuilderConfig(allow_multiple_marker_tasks=True))
        source = (
            "theorem one : True := by\n"
            "  {{proof}}\n\n"
            "theorem two : True := by\n"
            "  {{proof}}\n"
        )

        tasks = builder.build_from_source(source, task_id_prefix="basic")

        self.assertEqual(len(tasks), 2)
        self.assertEqual([task.metadata["task_name"] for task in tasks], ["basic.one", "basic.two"])
        self.assertEqual([task.task_id for task in tasks], ["basic.one", "basic.two"])
        self.assertEqual(tasks[0].metadata["hole_line"], 2)
        self.assertEqual(tasks[1].metadata["hole_line"], 5)
        self.assertEqual(tasks[0].source_template.count("{{proof}}"), 1)
        self.assertEqual(tasks[1].source_template.count("{{proof}}"), 1)
        self.assertEqual(tasks[0].source_template.count("sorry"), 0)
        self.assertEqual(tasks[1].source_template.count("sorry"), 0)
        self.assertIn("{{dependency:basic.one}}", tasks[1].source_template)
        self.assertEqual(tasks[0].metadata["source_hole_count"], 2)

    def test_ignores_sorry_inside_comments_and_strings(self) -> None:
        builder = LeanTaskBuilder()
        source = (
            "-- sorry in a line comment\n"
            "/- sorry in a block comment -/\n"
            'def word := "sorry"\n'
            "theorem sample : True := by\n"
            "  sorry\n"
        )

        tasks = builder.build_from_source(source)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].metadata["hole_line"], 5)

    def test_raises_when_no_hole_exists(self) -> None:
        builder = LeanTaskBuilder()

        with self.assertRaises(TaskBuildError):
            builder.build_from_source("theorem sample : True := by\n  trivial\n")

    def test_build_from_file_records_absolute_source_path(self) -> None:
        builder = LeanTaskBuilder(TaskBuilderConfig(default_split="train"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Basic.lean"
            path.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")

            tasks = builder.build_from_file(path)

        self.assertEqual(len(tasks), 1)
        self.assertTrue(Path(tasks[0].metadata["source_file"]).is_absolute())
        self.assertEqual(tasks[0].metadata["split"], "train")

    def test_jsonl_export_round_trips_basic_fields(self) -> None:
        builder = LeanTaskBuilder()
        tasks = builder.build_from_source("theorem sample : True := by\n  sorry\n")

        payload = builder.to_jsonl(tasks)
        row = json.loads(payload)

        self.assertEqual(row["task_id"], tasks[0].task_id)
        self.assertEqual(row["hole_marker"], "{{proof}}")
        self.assertIn("source_template", row)

    def test_builder_output_renders_with_candidate_edit(self) -> None:
        builder = LeanTaskBuilder()
        task = builder.build_from_source("theorem sample : True := by\n  sorry\n")[0]

        rendered = task.source_template.replace(task.hole_marker, CandidateEdit("trivial").text)

        self.assertIn("trivial", rendered)
        self.assertNotIn("{{proof}}", rendered)


if __name__ == "__main__":
    unittest.main()
