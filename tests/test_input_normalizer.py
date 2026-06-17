from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent import TaskInputKind
from agent.agents import FormalizationResult, StaticFormalizationAgent
from agent.input.normalizer import InputNormalizer, NormalizedInput, prepare_tasks
from agent.tasks.task_builder import LeanTaskBuilder, TaskBuilderConfig
from agent.tasks.types import TaskInputSpec


class ResolveKindTests(unittest.TestCase):
    def test_respects_explicit_input_kind(self) -> None:
        n = InputNormalizer()
        self.assertEqual(n.resolve_kind(input_kind="natural_language"), TaskInputKind.NATURAL_LANGUAGE)
        self.assertEqual(n.resolve_kind(input_kind="lean"), TaskInputKind.LEAN)
        self.assertEqual(n.resolve_kind(input_kind="auto"), TaskInputKind.LEAN)

    def test_detects_direct_natural_language_input(self) -> None:
        n = InputNormalizer()
        self.assertEqual(n.resolve_kind(problem="Prove True."), TaskInputKind.NATURAL_LANGUAGE)

    def test_detects_natural_language_suffix(self) -> None:
        n = InputNormalizer()
        for suffix in (".txt", ".md", ".tex"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    n.resolve_kind(source=f"problem{suffix}", input_kind="auto"),
                    TaskInputKind.NATURAL_LANGUAGE,
                )

    def test_explicit_lean_overrides_natural_language_suffix(self) -> None:
        n = InputNormalizer()
        self.assertEqual(
            n.resolve_kind(source="problem.txt", input_kind="lean"),
            TaskInputKind.LEAN,
        )

    def test_prefers_config_content_over_cli_flag(self) -> None:
        n = InputNormalizer()
        config_with_source = {"tasks": [{"proof_source": "theorem sample : True := by sorry"}]}
        config_with_problem = {"tasks": [{"problem": "Prove True."}]}

        self.assertEqual(
            n.resolve_kind(input_kind="natural_language", config=config_with_source),
            TaskInputKind.LEAN,
        )
        self.assertEqual(
            n.resolve_kind(input_kind="lean", config=config_with_problem),
            TaskInputKind.NATURAL_LANGUAGE,
        )


class NormalizeTests(unittest.TestCase):
    def test_inline_lean_config(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(
            task_config={
                "tasks": [
                    {
                        "task_id": "sample",
                        "source_name": "inline.lean",
                        "proof_source": "theorem sample : True := by\n  sorry\n",
                    }
                ]
            },
            task_config_path="config.json",
        )

        self.assertEqual(normalized.kind, TaskInputKind.LEAN)
        self.assertEqual(len(normalized.specs), 1)
        spec = normalized.specs[0]
        self.assertEqual(spec.kind, TaskInputKind.LEAN)
        self.assertEqual(spec.text, "theorem sample : True := by\n  sorry\n")
        self.assertEqual(spec.source_name, "inline.lean")
        self.assertEqual(spec.metadata["task_source_kind"], "inline")
        self.assertEqual(spec.metadata["task_config_file"], "config.json")

    def test_nl_config(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(
            task_config={
                "problem": "Prove True.",
                "informal_proof": "Use trivial.",
                "tasks": [{"task_id": "sample"}],
            },
            task_config_path="nl.json",
        )

        self.assertEqual(normalized.kind, TaskInputKind.NATURAL_LANGUAGE)
        self.assertEqual(len(normalized.specs), 1)
        spec = normalized.specs[0]
        self.assertEqual(spec.kind, TaskInputKind.NATURAL_LANGUAGE)
        self.assertEqual(spec.text, "Prove True.")
        self.assertEqual(spec.informal_proof, "Use trivial.")
        self.assertEqual(spec.metadata["task_source_kind"], "natural_language")

    def test_direct_problem_string(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(problem="Prove True.")

        self.assertEqual(normalized.kind, TaskInputKind.NATURAL_LANGUAGE)
        self.assertEqual(normalized.specs[0].text, "Prove True.")
        self.assertEqual(normalized.specs[0].source_name, "cli:problem")

    def test_problem_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "problem.md"
            path.write_text("Prove False.\n", encoding="utf-8")
            n = InputNormalizer()
            normalized = n.normalize(problem_file=str(path))

        self.assertEqual(normalized.kind, TaskInputKind.NATURAL_LANGUAGE)
        self.assertEqual(normalized.specs[0].text, "Prove False.\n")
        self.assertEqual(normalized.specs[0].source_name, str(path))

    def test_lean_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Basic.lean"
            path.write_text("theorem sample : True := by\n  sorry\n", encoding="utf-8")
            n = InputNormalizer()
            normalized = n.normalize(source=str(path))

        self.assertEqual(normalized.kind, TaskInputKind.LEAN)
        spec = normalized.specs[0]
        self.assertEqual(spec.kind, TaskInputKind.LEAN)
        self.assertFalse(spec.is_directory)
        self.assertEqual(spec.source_path, str(path.resolve()))

    def test_lean_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.lean").write_text("theorem a : True := by sorry\n", encoding="utf-8")
            (root / "b.lean").write_text("theorem b : True := by sorry\n", encoding="utf-8")
            n = InputNormalizer()
            normalized = n.normalize(source=str(root), pattern="*.lean")

        self.assertEqual(normalized.kind, TaskInputKind.LEAN)
        spec = normalized.specs[0]
        self.assertTrue(spec.is_directory)
        self.assertEqual(spec.directory_pattern, "*.lean")
        self.assertEqual(spec.source_path, str(root.resolve()))

    def test_natural_language_without_input_raises(self) -> None:
        n = InputNormalizer()
        with self.assertRaises(ValueError):
            n.normalize(source=None, input_kind="natural_language")


class PrepareTasksTests(unittest.TestCase):
    def test_prepare_inline_lean(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(
            task_config={
                "tasks": [
                    {
                        "task_id": "sample",
                        "proof_source": "theorem sample : True := by\n  sorry\n",
                    }
                ]
            }
        )
        builder = LeanTaskBuilder(TaskBuilderConfig())

        tasks = prepare_tasks(normalized, builder=builder, formalizer=None)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].metadata["input_kind"], "lean")

    def test_prepare_nl_with_static_formalizer(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(
            task_config={
                "problem": "Prove True.",
                "tasks": [{"task_id": "sample"}],
            }
        )
        builder = LeanTaskBuilder(TaskBuilderConfig())
        formalizer = StaticFormalizationAgent(
            FormalizationResult(
                proof_source="theorem sample : True := by\n  sorry\n",
                natural_language_proof="True is trivial.",
                metadata={"model": "static"},
            )
        )

        tasks = prepare_tasks(normalized, builder=builder, formalizer=formalizer)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].metadata["input_kind"], "natural_language")
        self.assertEqual(tasks[0].metadata["natural_language_problem"], "Prove True.")
        self.assertEqual(tasks[0].metadata["natural_language_proof"], "True is trivial.")

    def test_prepare_nl_without_formalizer_raises(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(task_config={"problem": "Prove True.", "tasks": [{"task_id": "sample"}]})
        builder = LeanTaskBuilder(TaskBuilderConfig())

        from agent.tasks.task_builder import TaskBuildError

        with self.assertRaises(TaskBuildError):
            prepare_tasks(normalized, builder=builder, formalizer=None)

    def test_prepare_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.lean").write_text("theorem a : True := by sorry\n", encoding="utf-8")
            (root / "b.lean").write_text("theorem b : True := by sorry\n", encoding="utf-8")
            n = InputNormalizer()
            normalized = n.normalize(source=str(root), pattern="*.lean")
            builder = LeanTaskBuilder(TaskBuilderConfig())

            tasks = prepare_tasks(normalized, builder=builder, formalizer=None)

        self.assertEqual(len(tasks), 2)

    def test_prepare_imports_merged(self) -> None:
        n = InputNormalizer()
        normalized = n.normalize(
            task_config={
                "imports": ["Mathlib.Logic.Basic"],
                "tasks": [
                    {
                        "task_id": "sample",
                        "proof_source": "theorem sample : True := by\n  sorry\n",
                    }
                ],
            }
        )
        builder = LeanTaskBuilder(TaskBuilderConfig())

        tasks = prepare_tasks(normalized, builder=builder, formalizer=None)

        self.assertEqual(tasks[0].imports, ("Mathlib.Logic.Basic",))


if __name__ == "__main__":
    unittest.main()
