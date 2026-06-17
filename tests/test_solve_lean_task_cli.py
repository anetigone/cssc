from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from agent.search.action import StaticActionGenerator
from agent import TaskInputKind
from agent.cli.output import result_payload
from agent.cli.config import apply_task_config
from agent.cli.generators import build_action_generator
from agent.cli.paths import find_lake_root, resolve_agent_path
from agent.cli.solve_lean_task import _run_artifact_path
from agent.cli.tasks import build_tasks, classify_input, select_task
from agent.cli.workspace import build_check_workspace, _workspace_context
from agent.agents import FormalizationResult, StaticFormalizationAgent
from agent.proof_system.base import CandidateEdit, CheckResult, DiagnosticCategory
from agent.search.budget import BudgetSnapshot
from agent.search.controller import AttemptRecord, ControllerResult


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

    def test_static_candidate_generator_wins_even_when_model_enabled_for_formalizer(self) -> None:
        args = _args(candidate=["trivial"], use_model=True)

        generator = build_action_generator(args)

        self.assertIsInstance(generator, StaticActionGenerator)

    def test_model_generator_loads_env_only_when_file_exists(self) -> None:
        args = _args(use_model=True, env_file="missing.env")
        with patch("agent.cli.generators.load_dotenv") as load_mock:
            with patch(
                "agent.cli.generators.OpenAIChatConfig.from_env",
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

    def test_task_config_attaches_natural_language_problem_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "data" / "tasks"
            task_dir.mkdir(parents=True)
            config = task_dir / "basic.json"
            config.write_text(
                json_config(
                    {
                        "problem": "Prove that True is true.",
                        "informal_proof": "The proposition True is inhabited by trivial.",
                        "tasks": [
                            {
                                "task_id": "sample",
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

        self.assertEqual(tasks[0].metadata["natural_language_problem"], "Prove that True is true.")
        self.assertEqual(
            tasks[0].metadata["natural_language_proof"],
            "The proposition True is inhabited by trivial.",
        )

    def test_task_config_with_only_natural_language_uses_formalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "data" / "tasks"
            task_dir.mkdir(parents=True)
            config = task_dir / "nl.json"
            config.write_text(
                json_config(
                    {
                        "problem": "Prove that True is true.",
                        "tasks": [{"task_id": "sample"}],
                    }
                ),
                encoding="utf-8",
            )
            args = _args(source=None, agent_root=str(root), task_config="data/tasks/nl.json")
            configured = apply_task_config(args)
            formalizer = StaticFormalizationAgent(
                FormalizationResult(
                    proof_source="theorem sample : True := by\n  sorry\n",
                    natural_language_proof="True is proved by trivial.",
                    metadata={"model": "static"},
                )
            )

            tasks = build_tasks(configured, formalizer=formalizer)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].metadata["input_kind"], "natural_language")
        self.assertEqual(tasks[0].metadata["natural_language_problem"], "Prove that True is true.")
        self.assertEqual(tasks[0].metadata["natural_language_proof"], "True is proved by trivial.")
        self.assertEqual(formalizer.requests[0].problem, "Prove that True is true.")

    def test_result_payload_includes_natural_language_proof_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = build_tasks(
                _args(
                    source=None,
                    _task_config_data={
                        "tasks": [
                            {
                                "task_id": "sample",
                                "problem": "Prove True.",
                                "informal_proof": "Use the canonical proof of True.",
                                "proof_source": "theorem sample : True := by\n  sorry\n",
                            }
                        ]
                    },
                    _task_config_path="inline",
                )
            )[0]
            attempt_file = Path(tmp) / "candidate.lean"
            attempt = AttemptRecord(
                attempt_index=0,
                candidate_id="candidate",
                edit=CandidateEdit("trivial"),
                candidate_file=attempt_file,
                check_result=CheckResult(
                    accepted=True,
                    category=DiagnosticCategory.PROOF_ACCEPTED,
                    raw_output="",
                    candidate_file=attempt_file,
                ),
            )
            result = ControllerResult(
                task=task,
                accepted=True,
                attempts=(attempt,),
                budget=BudgetSnapshot(
                    checks_used=1,
                    model_calls_used=0,
                    elapsed_seconds=0.0,
                    remaining_checks=0,
                    remaining_model_calls=0,
                    exhausted_reason=None,
                ),
                stop_reason="accepted",
                accepted_attempt=attempt,
            )

            payload = result_payload(result)

        self.assertEqual(payload["natural_language_problem"], "Prove True.")
        self.assertEqual(payload["natural_language_proof"], "Use the canonical proof of True.")
        self.assertEqual(payload["accepted_proof"], "trivial")

    def test_task_config_rejects_unknown_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "data" / "tasks"
            task_dir.mkdir(parents=True)
            config = task_dir / "basic.json"
            config.write_text(
                json_config({"project_root": "lean_workspace", "retrival_source": "Basic.lean"}),
                encoding="utf-8",
            )
            args = _args(agent_root=str(root), task_config="data/tasks/basic.json")

            with self.assertRaises(ValueError) as ctx:
                apply_task_config(args)

        self.assertIn("retrival_source", str(ctx.exception))

    def test_default_workspace_is_under_agent_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with _workspace_context(None, agent_root=root) as work_dir:
                expected = resolve_agent_path(root, ".runs")
                self.assertEqual(work_dir, expected)
                self.assertTrue(expected.exists())

    def test_run_artifact_path_groups_loose_runs_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            log_path = _run_artifact_path(root, ".runs/sup_sequence_e2e.log")
            trace_path = _run_artifact_path(root, ".runs/sup_sequence_e2e_trace.jsonl")

        self.assertEqual(log_path, root / ".runs" / "sup_sequence_e2e" / "sup_sequence_e2e.log")
        self.assertEqual(
            trace_path,
            root / ".runs" / "sup_sequence_e2e" / "sup_sequence_e2e_trace.jsonl",
        )

    def test_default_check_workspace_is_under_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "lean_workspace"
            args = _args()

            workspace = build_check_workspace(args, agent_root=root, project_root=project_root)

        self.assertIsNotNone(workspace)
        self.assertEqual(workspace.root, (project_root / ".checks").resolve())

    def test_classify_input_respects_explicit_input_kind(self) -> None:
        self.assertEqual(classify_input(_args(input_kind="natural_language")), TaskInputKind.NATURAL_LANGUAGE)
        self.assertEqual(classify_input(_args(input_kind="lean")), TaskInputKind.LEAN)
        self.assertEqual(classify_input(_args(input_kind="auto")), TaskInputKind.LEAN)

    def test_classify_input_detects_direct_natural_language_input(self) -> None:
        self.assertEqual(classify_input(_args(problem="Prove True.")), TaskInputKind.NATURAL_LANGUAGE)

    def test_classify_input_detects_natural_language_suffix(self) -> None:
        for suffix in (".txt", ".md", ".tex"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    classify_input(_args(source=f"problem{suffix}", input_kind="auto")),
                    TaskInputKind.NATURAL_LANGUAGE,
                )

    def test_classify_input_explicit_lean_overrides_natural_language_suffix(self) -> None:
        self.assertEqual(
            classify_input(_args(source="problem.txt", input_kind="lean")),
            TaskInputKind.LEAN,
        )

    def test_classify_input_prefers_config_content_over_cli_flag(self) -> None:
        config_with_source = {"tasks": [{"proof_source": "theorem sample : True := by sorry"}]}
        config_with_problem = {"tasks": [{"problem": "Prove True."}]}

        self.assertEqual(
            classify_input(_args(input_kind="natural_language"), config_with_source),
            TaskInputKind.LEAN,
        )
        self.assertEqual(
            classify_input(_args(input_kind="lean"), config_with_problem),
            TaskInputKind.NATURAL_LANGUAGE,
        )

    def test_build_tasks_with_input_kind_lean_and_txt_source_uses_lean_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lean_in_txt.txt"
            path.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")
            args = _args(source=str(path), input_kind="lean")

            tasks = build_tasks(args)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].metadata["input_kind"], "lean")

    def test_build_tasks_input_kind_natural_language_without_input_raises(self) -> None:
        args = _args(source=None, input_kind="natural_language")

        with self.assertRaises(ValueError) as ctx:
            build_tasks(args)

        self.assertIn("natural-language problem", str(ctx.exception).lower())


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
        "input_kind": "auto",
        "problem": None,
        "problem_file": None,
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
