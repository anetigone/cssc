from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from agent.search.action import StaticActionGenerator
from agent.cli.solve_lean_task import (
    apply_task_config,
    build_action_generator,
    build_check_workspace,
    build_tasks,
    find_lake_root,
    resolve_agent_path,
    select_task,
    _workspace_context,
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

    def test_task_config_supplies_root_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "data" / "tasks"
            task_dir.mkdir(parents=True)
            lean_dir = root / "lean_workspace"
            lean_dir.mkdir()
            config = task_dir / "basic.json"
            config.write_text(
                json_config(
                    {
                        "project_root": "lean_workspace",
                        "imports": ["LeanWorkspace.Basic"],
                        "enable_retrieval": True,
                        "tasks": [
                            {
                                "task_id": "sample",
                                "source_name": "data/tasks/basic.json#sample",
                                "proof_source": "theorem sample : True := by\n  sorry\n",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = _args(source=None, agent_root=str(root), task_config="data/tasks/basic.json")

            configured = apply_task_config(args)
            tasks = build_tasks(configured)

        self.assertIsNone(configured.source)
        self.assertEqual(configured.project_root, "lean_workspace")
        self.assertTrue(configured.enable_retrieval)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].metadata["task_source_kind"], "inline")
        self.assertEqual(tasks[0].metadata["task_config_index"], 0)
        self.assertEqual(tasks[0].imports, ("LeanWorkspace.Basic",))
        self.assertNotIn("import LeanWorkspace.Basic", tasks[0].source_template)

    def test_default_workspace_is_under_agent_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with _workspace_context(None, agent_root=root) as work_dir:
                expected = resolve_agent_path(root, ".runs")
                self.assertEqual(work_dir, expected)
                self.assertTrue(expected.exists())

    def test_default_check_workspace_is_under_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "lean_workspace"
            args = _args()

            workspace = build_check_workspace(args, agent_root=root, project_root=project_root)

        self.assertIsNotNone(workspace)
        self.assertEqual(workspace.root, (project_root / ".checks").resolve())


def _args(**overrides) -> Namespace:
    values = {
        "source": "Basic.lean",
        "agent_root": ".",
        "task_config": None,
        "project_root": None,
        "split": None,
        "task_id": None,
        "task_index": 0,
        "hole_marker": "{{proof}}",
        "allow_multiple_marker_tasks": False,
        "allow_multiple_sorry_tasks": False,
        "inactive_hole_fill": "sorry",
        "pattern": "*.lean",
        "candidate": [],
        "candidate_file": [],
        "use_model": False,
        "env_file": ".env",
        "enable_retrieval": False,
        "retrieval_source": [],
        "max_retrieval_results": 5,
        "retrieve_before_first_model_call": False,
        "no_lake": False,
        "check_work_dir": None,
        "keep_check_files": False,
    }
    values.update(overrides)
    return Namespace(**values)


def json_config(value: dict) -> str:
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
