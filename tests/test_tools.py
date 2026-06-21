from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

from agent.agents import (
    FunctionTool,
    LeanEnvironmentToolProvider,
    LeanProofToolProvider,
    ToolCall,
    extract_missing_imports,
    extract_tool_calls,
)


class ExtractToolCallsTests(unittest.TestCase):
    def test_extracts_openai_style_tool_calls(self) -> None:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_available_modules",
                        "arguments": '{"limit": 10}',
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "check_module_exists",
                        "arguments": '{"module": "Mathlib.Data.Nat.Basic"}',
                    },
                },
            ],
        }
        calls = extract_tool_calls(message)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ToolCall(id="call_1", name="list_available_modules", arguments={"limit": 10}))
        self.assertEqual(
            calls[1],
            ToolCall(id="call_2", name="check_module_exists", arguments={"module": "Mathlib.Data.Nat.Basic"}),
        )

    def test_returns_empty_when_no_tool_calls(self) -> None:
        self.assertEqual(extract_tool_calls({"role": "assistant", "content": "hello"}), ())
        self.assertEqual(extract_tool_calls({}), ())

    def test_skips_malformed_tool_calls(self) -> None:
        message = {
            "tool_calls": [
                {"id": "call_1", "function": {"name": "ok", "arguments": "{}"}},
                {"id": "call_2"},
                {"function": {"name": "ok2", "arguments": "{}"}},
            ]
        }
        calls = extract_tool_calls(message)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_1")


class ExtractMissingImportsTests(unittest.TestCase):
    def test_extracts_unknown_package(self) -> None:
        raw = "error: unknown package 'Mathlib.Topology.Instances.Real'"
        self.assertEqual(extract_missing_imports(raw), ("Mathlib.Topology.Instances.Real",))

    def test_extracts_unknown_module(self) -> None:
        raw = "error: unknown module 'Foo.Bar.Baz'\nerror: unknown module 'Foo.Bar.Qux'"
        self.assertEqual(
            extract_missing_imports(raw),
            ("Foo.Bar.Baz", "Foo.Bar.Qux"),
        )

    def test_returns_empty_when_no_match(self) -> None:
        self.assertEqual(extract_missing_imports("type mismatch application"), ())

    def test_does_not_extract_non_import_could_not_find(self) -> None:
        self.assertEqual(
            extract_missing_imports("could not find instance 'Foo'"),
            (),
        )
        self.assertEqual(
            extract_missing_imports("could not find local declaration 'foo'"),
            (),
        )


class FunctionToolTests(unittest.TestCase):
    def test_openai_schema(self) -> None:
        tool = FunctionTool(
            name="test_tool",
            description="A test tool.",
            parameters={"type": "object", "properties": {}},
            _execute=lambda args: json.dumps({"ok": True}),
        )
        schema = tool.openai_schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "test_tool")
        self.assertEqual(schema["function"]["description"], "A test tool.")

    def test_execute(self) -> None:
        tool = FunctionTool(
            name="echo",
            description="Echo.",
            parameters={},
            _execute=lambda args: json.dumps(args),
        )
        self.assertEqual(tool.execute({"x": 1}), '{"x": 1}')


class LeanProofToolProviderTests(unittest.TestCase):
    def test_check_snippet_returns_bounded_json_and_removes_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lakefile.toml").write_text('name = "sample"\n', encoding="utf-8")
            provider = LeanProofToolProvider(root, lake_executable="lake")
            completed = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="Attempt.lean:1:1: error: bad"
            )
            with patch("agent.agents.tools.lean_proof.subprocess.run", return_value=completed) as run:
                result = json.loads(provider.tools()[0].execute({"code": "#check Missing"}))

            self.assertFalse(result["ok"])
            self.assertIn("error: bad", result["output"])
            self.assertEqual(list(root.glob("proof_tool_*.lean")), [])
            self.assertEqual(run.call_args.kwargs["cwd"], root)

    def test_check_snippet_rejects_oversized_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = LeanProofToolProvider(tmp, max_source_chars=4)
            result = json.loads(provider.tools()[0].execute({"code": "12345"}))
        self.assertFalse(result["ok"])
        self.assertIn("too large", result["error"])


class LeanEnvironmentToolProviderTests(unittest.TestCase):
    def test_discovers_project_local_modules(self) -> None:
        project_root = Path(__file__).parent.parent / "lean_test"
        provider = LeanEnvironmentToolProvider(project_root=project_root)
        modules = provider._discover_modules()
        self.assertIn("LeanTest.Basic", modules)
        self.assertIn("LeanTest.Integration", modules)
        self.assertIn("Init", modules)
        self.assertIn("Std", modules)
        self.assertIn("Lean", modules)

    def test_list_available_modules_tool_returns_json(self) -> None:
        project_root = Path(__file__).parent.parent / "lean_test"
        provider = LeanEnvironmentToolProvider(project_root=project_root)
        tools = {tool.name: tool for tool in provider.tools()}
        result = tools["list_available_modules"].execute({"limit": 100})
        data = json.loads(result)
        self.assertIn("LeanTest.Basic", data["modules"])
        self.assertEqual(data["project_root"], str(project_root.resolve()))

    def test_list_available_modules_respects_limit(self) -> None:
        provider = LeanEnvironmentToolProvider()
        tools = {tool.name: tool for tool in provider.tools()}
        result = tools["list_available_modules"].execute({"limit": 2})
        data = json.loads(result)
        self.assertLessEqual(len(data["modules"]), 2)

    def test_check_module_timeout_is_not_reported_as_missing(self) -> None:
        provider = LeanEnvironmentToolProvider(import_check_timeout_seconds=0.01)
        tools = {tool.name: tool for tool in provider.tools()}

        with patch.object(provider, "_check_import_compiles", return_value=None):
            result = tools["check_module_exists"].execute({"module": "Mathlib.Data.Nat.Basic"})

        data = json.loads(result)
        self.assertIsNone(data["exists"])
        self.assertIn("timed out", data["error"])

    @unittest.skipUnless(shutil.which("lake") and shutil.which("lean"), "Lean toolchain not available")
    def test_check_module_exists_true_for_local_module(self) -> None:
        project_root = Path(__file__).parent.parent / "lean_test"
        provider = LeanEnvironmentToolProvider(project_root=project_root)
        tools = {tool.name: tool for tool in provider.tools()}
        result = tools["check_module_exists"].execute({"module": "LeanTest.Basic"})
        data = json.loads(result)
        self.assertTrue(data["exists"], data)

    @unittest.skipUnless(shutil.which("lake") and shutil.which("lean"), "Lean toolchain not available")
    def test_check_module_exists_false_for_missing_module(self) -> None:
        project_root = Path(__file__).parent.parent / "lean_test"
        provider = LeanEnvironmentToolProvider(project_root=project_root)
        tools = {tool.name: tool for tool in provider.tools()}
        result = tools["check_module_exists"].execute({"module": "Definitely.Not.A.Module"})
        data = json.loads(result)
        self.assertFalse(data["exists"])


if __name__ == "__main__":
    unittest.main()
