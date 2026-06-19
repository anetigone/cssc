from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.search.action import StaticActionGenerator
from agent import TaskInputKind
from agent.cli.output import result_payload
from agent.cli.config import apply_task_config
from agent.cli.generators import build_action_generator
from agent.cli.parser import build_parser
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
                "agent.cli.generators.ChatConfig.from_env",
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

    def test_run_artifact_path_groups_under_run_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            log_path = _run_artifact_path(root, ".runs/sup_sequence_e2e.log", "sup_sequence_e2e")
            trace_path = _run_artifact_path(root, ".runs/sup_sequence_e2e_trace.jsonl", "sup_sequence_e2e")

        self.assertEqual(log_path, root / ".runs" / "sup_sequence_e2e" / "sup_sequence_e2e.log")
        self.assertEqual(
            trace_path,
            root / ".runs" / "sup_sequence_e2e" / "sup_sequence_e2e_trace.jsonl",
        )

    def test_run_artifact_path_without_run_name_is_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            log_path = _run_artifact_path(root, ".runs/sup_sequence_e2e.log", None)

        self.assertEqual(log_path, (root / ".runs" / "sup_sequence_e2e.log").resolve())

    def test_run_artifact_path_sanitizes_run_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            log_path = _run_artifact_path(root, ".runs/run.log", "my run/name")

        self.assertEqual(log_path, root / ".runs" / "my_run_name" / "run.log")

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


class CliSubcommandTests(unittest.TestCase):
    def test_parser_exposes_three_stage_commands(self) -> None:
        parser = build_parser()
        top_help = _format_help(parser, ["--help"])
        self.assertIn("solve", top_help)
        self.assertIn("formalize", top_help)
        self.assertIn("prove", top_help)

    def test_parser_scopes_stage_model_flags(self) -> None:
        parser = build_parser()
        solve_help = _format_help(parser, ["solve", "--help"])
        formalize_help = _format_help(parser, ["formalize", "--help"])
        prove_help = _format_help(parser, ["prove", "--help"])

        for flag in ("--formalizer-model", "--formalizer-temperature", "--formalizer-max-tokens"):
            self.assertIn(flag, solve_help)
            self.assertNotIn(flag, formalize_help)
        for flag in ("--proof-model", "--proof-temperature", "--proof-max-tokens"):
            self.assertIn(flag, solve_help)
            self.assertNotIn(flag, formalize_help)
        for stage_help in (formalize_help, prove_help):
            self.assertIn("--model", stage_help)
            self.assertIn("--temperature", stage_help)
            self.assertIn("--max-tokens", stage_help)
        self.assertNotIn("--repair-model", solve_help)

    def test_parser_parses_per_role_overrides(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "solve",
                "Basic.lean",
                "--formalizer-model",
                "f-model",
                "--formalizer-temperature",
                "0.1",
                "--proof-model",
                "p-model",
            ]
        )
        self.assertEqual(args.command, "solve")
        self.assertEqual(args.formalizer_model, "f-model")
        self.assertAlmostEqual(args.formalizer_temperature, 0.1)
        self.assertEqual(args.proof_model, "p-model")

    def test_formalize_and_prove_use_generic_stage_model_flags(self) -> None:
        parser = build_parser()
        formalize = parser.parse_args(["formalize", "--problem", "x", "--model", "f-model"])
        prove = parser.parse_args(["prove", "Basic.lean", "--model", "p-model"])
        self.assertEqual(formalize.model, "f-model")
        self.assertEqual(prove.model, "p-model")

    def test_no_model_explicitly_disables_model_calls(self) -> None:
        parser = build_parser()
        for command in (
            ["solve", "Basic.lean", "--no-model"],
            ["formalize", "--problem", "x", "--no-use-model"],
            ["prove", "Basic.lean", "--no-model"],
        ):
            self.assertIs(parser.parse_args(command).use_model, False)

    def test_no_model_gives_friendly_formalization_error(self) -> None:
        from agent.cli.generators import build_formalization_agent

        args = _args(
            source=None,
            input_kind="natural_language",
            problem="Prove True.",
            use_model=False,
        )
        with self.assertRaisesRegex(ValueError, "formalization requires a model"):
            build_formalization_agent(args)

    def test_model_config_honours_role_overrides(self) -> None:
        from agent.cli.generators import _model_config

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "env-model"}),
            patch("agent.cli.generators.load_dotenv"),
        ):
            formalizer_cfg = _model_config(_args(use_model=True, formalizer_model="f-model"), role="formalizer")
            proof_cfg = _model_config(_args(use_model=True), role="proof")

        self.assertEqual(formalizer_cfg.model, "f-model")
        self.assertEqual(proof_cfg.model, "env-model")  # proof role falls back to env

    def test_model_config_temperature_override_flows_into_config(self) -> None:
        from agent.cli.generators import _model_config

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "env-model"}),
            patch("agent.cli.generators.load_dotenv"),
        ):
            cfg = _model_config(_args(use_model=True, formalizer_temperature=0.0), role="formalizer")

        self.assertEqual(cfg.temperature, 0.0)

    def test_proof_generator_attaches_only_proof_tools(self) -> None:
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "env-model"}),
            patch("agent.cli.generators.load_dotenv"),
            patch("agent.cli.generators._lean_tools") as lean_tools,
            patch("agent.cli.generators._proof_tools", return_value=(MagicMock(name="proof_tool"),)) as proof_tools,
        ):
            generator = build_action_generator(_args(use_model=True), project_root=Path("."))

        lean_tools.assert_not_called()
        proof_tools.assert_called_once()
        self.assertEqual(len(generator.driver.tools), 1)

    def test_run_formalize_rejects_lean_input(self) -> None:
        from agent.cli import solve_lean_task as cli

        args = _args(source="Basic.lean", input_kind="lean")
        with patch.object(cli, "_lean_services") as services_ctx:
            services_ctx.return_value.__enter__.return_value = cli._LeanServices(
                adapter=object(), validation_adapter=object()
            )
            with patch.object(cli, "_workspace_context") as ws_ctx:
                ws_ctx.return_value.__enter__.return_value = Path(".")
                rc = cli.run_formalize(args, agent_root=Path("."), project_root=None)

        self.assertEqual(rc, 2)

    def test_run_formalize_prints_scaffold_for_natural_language(self) -> None:
        from agent.cli import solve_lean_task as cli

        args = _args(
            command="formalize",
            source=None,
            input_kind="natural_language",
            problem="Prove True.",
            use_model=True,
            no_check=True,
            all_tasks=False,
        )
        formalizer = StaticFormalizationAgent(
            FormalizationResult(
                proof_source="theorem sample : True := by\n  sorry\n",
                natural_language_proof="trivial",
                metadata={"model": "static"},
            )
        )
        with (
            patch.object(cli, "build_formalization_agent", return_value=formalizer),
            patch("builtins.print") as printed,
        ):
            rc = cli.run_formalize(args, agent_root=Path("."), project_root=None)

        self.assertEqual(rc, 0)
        payload = json.loads(printed.call_args.args[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stage"], "formalize")
        artifact = payload["tasks"][0]
        self.assertEqual(artifact["source_template"], "theorem sample : True := by\n  sorry\n")
        self.assertEqual(artifact["metadata"]["natural_language_proof"], "trivial")

    def test_formalize_list_tasks_does_not_build_model_or_lean(self) -> None:
        from agent.cli import solve_lean_task as cli

        args = _args(
            command="formalize",
            source=None,
            input_kind="natural_language",
            problem="Prove True.",
            list_tasks=True,
            all_tasks=False,
        )
        with (
            patch.object(cli, "build_formalization_agent") as build_agent,
            patch.object(cli, "_lean_services") as lean_services,
            patch("builtins.print"),
        ):
            rc = cli.run_formalize(args, agent_root=Path("."), project_root=None)
        self.assertEqual(rc, 0)
        build_agent.assert_not_called()
        lean_services.assert_not_called()

    def test_positional_json_is_promoted_for_prove(self) -> None:
        from agent.cli.solve_lean_task import _promote_positional_artifact

        args = Namespace(command="prove", source="scaffold.json", task_config=None)
        _promote_positional_artifact(args)
        self.assertEqual(args.task_config, "scaffold.json")
        self.assertIsNone(args.source)


def _format_help(parser, argv) -> str:
    """Render a subcommand help string without raising SystemExit."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            parser.parse_args(argv)
        except SystemExit:
            pass
    return buf.getvalue()


def _args(**overrides) -> Namespace:
    values = {
        "command": "solve",
        "source": "Basic.lean",
        "agent_root": ".",
        "task_config": None,
        "list_tasks": False,
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
        "max_candidates": 1,
        "max_model_calls": 3,
        "no_lake": False,
        "no_lean_server": False,
        "allow_sorry": False,
        "lean_timeout": 10.0,
        "scaffold_timeout": None,
        "check_work_dir": None,
        "keep_check_files": False,
        "max_checks": 3,
        "max_elapsed_seconds": None,
        "work_dir": None,
        "model_timeout": 60.0,
        "model_max_tokens": 16384,
        "lean_server_startup_timeout": 60.0,
        "formalization_cache_dir": None,
        "formalization_cache": False,
        "no_formalization_cache": False,
        "run_name": None,
        "trace_jsonl": None,
        "trace_raw_output": False,
        "formalizer_model": None,
        "formalizer_temperature": None,
        "formalizer_max_tokens": None,
        "proof_model": None,
        "proof_temperature": None,
        "proof_max_tokens": None,
        "repair_model": None,
        "repair_temperature": None,
        "repair_max_tokens": None,
    }
    values.update(overrides)
    return Namespace(**values)


def json_config(value: dict) -> str:
    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
