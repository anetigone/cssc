from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from agent.search.action import StaticActionGenerator
from agent.cli.solve_lean_task import (
    build_action_generator,
    build_tasks,
    find_lake_root,
    select_task,
)


class SolveLeanTaskCliTests(unittest.TestCase):
    def test_build_tasks_from_file_and_select_by_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Basic.lean"
            path.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")
            args = _args(source=str(path))

            tasks = build_tasks(args)
            selected = select_task(tasks, task_index=0)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(selected.task_id, tasks[0].task_id)
        self.assertEqual(selected.metadata["hole_kind"], "sorry")

    def test_select_task_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Basic.lean"
            path.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")
            tasks = build_tasks(_args(source=str(path)))

        selected = select_task(tasks, task_id=tasks[0].task_id)

        self.assertEqual(selected, tasks[0])

    def test_static_candidate_generator_reads_candidate_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate_file = Path(tmp) / "candidate.lean"
            candidate_file.write_text("trivial\n", encoding="utf-8")
            args = _args(candidate_file=[str(candidate_file)])

            generator = build_action_generator(args)

        self.assertIsInstance(generator, StaticActionGenerator)

    def test_model_generator_loads_env_only_when_file_exists(self) -> None:
        args = _args(use_model=True, env_file="missing.env")
        with patch("agent.cli.solve_lean_task.load_dotenv") as load_mock:
            with patch(
                "agent.cli.solve_lean_task.OpenAIChatConfig.from_env",
                side_effect=RuntimeError("stop"),
            ):
                with self.assertRaises(RuntimeError):
                    build_action_generator(args)

        load_mock.assert_not_called()

    def test_finds_lake_root_from_nested_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
            nested = root / "Cssc" / "Tasks"
            nested.mkdir(parents=True)
            lean_file = nested / "Basic.lean"
            lean_file.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")

            found = find_lake_root(str(lean_file))

        self.assertEqual(found, root.resolve())


def _args(**overrides) -> Namespace:
    values = {
        "source": "Basic.lean",
        "split": None,
        "hole_marker": "{{proof}}",
        "allow_multiple_marker_tasks": False,
        "allow_multiple_sorry_tasks": False,
        "inactive_hole_fill": "sorry",
        "pattern": "*.lean",
        "candidate": [],
        "candidate_file": [],
        "use_model": False,
        "env_file": ".env",
    }
    values.update(overrides)
    return Namespace(**values)


if __name__ == "__main__":
    unittest.main()
