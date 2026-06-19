"""Lean environment tools for formalization and proof agents.

Formalizers can inspect available modules. Proof agents can compile bounded
scratch snippets so library exploration does not leak into final proof bodies.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..proof_system.lean_project import LakeProject
from ..utils import resolve_executable
from .openai import (
    ChatConfig,
    ChatTransport,
    ModelAdapterError,
    chat_completions_url,
)


logger = logging.getLogger(__name__)

# Lean error messages that indicate a missing package/module. The regexes are
# intentionally conservative; new variants can be added as they are observed.
_MISSING_IMPORT_RES = (
    re.compile(r"unknown package ['\"](?P<name>[^'\"]+)['\"]"),
    re.compile(r"unknown module ['\"](?P<name>[^'\"]+)['\"]"),
    re.compile(r"unknown package\s+(?P<name>\S+)"),
    re.compile(r"unknown module\s+(?P<name>\S+)"),
    # Keep "could not find" narrow so that messages such as
    # "could not find instance ..." are not reported as missing imports.
    re.compile(r"could not find\s+(?:module|import|package)\s+['\"](?P<name>[^'\"]+)['\"]"),
    re.compile(r"could not find\s+(?:module|import|package)\s+(?P<name>\S+)"),
)


@dataclass(frozen=True)
class ToolCall:
    """One function-call request emitted by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of executing one tool call, ready to send back to the model."""

    call_id: str
    content: str


class Tool(Protocol):
    """Protocol for tools usable by the formalizer."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]: ...

    def openai_schema(self) -> dict[str, Any]: ...

    def execute(self, arguments: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    """A concrete tool backed by a Python callable."""

    name: str
    description: str
    parameters: dict[str, Any]
    _execute: Callable[[dict[str, Any]], str]

    def execute(self, arguments: dict[str, Any]) -> str:
        return self._execute(arguments)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class LeanEnvironmentToolProvider:
    """Provide tools that inspect the local Lean / Lake environment.

    The provider tries to discover:

    - core Lean modules (``Init``, ``Std``, ``Lean``);
    - the current Lake project's own modules;
    - packages declared in ``lake-manifest.json`` or ``lakefile.*``.

    It also offers a verification tool that compiles a single ``import X``
    snippet to confirm a module is resolvable.
    """

    def __init__(
        self,
        project_root: str | Path | None = None,
        lake_executable: str | None = None,
        lean_executable: str | None = None,
        import_check_timeout_seconds: float = 60.0,
    ) -> None:
        self.project_root = Path(project_root).resolve() if project_root else None
        self.lake_executable = resolve_executable(lake_executable, "lake")
        self.lean_executable = resolve_executable(lean_executable, "lean")
        self.import_check_timeout_seconds = import_check_timeout_seconds
        self._project = LakeProject(self.project_root)

    def tools(self) -> tuple[Tool, ...]:
        return (
            FunctionTool(
                name="list_available_modules",
                description=(
                    "List Lean module names that are known to be available in the "
                    "local environment. Prefer imports from this list when building "
                    "a scaffold. The bare 'import Mathlib' is forbidden; choose narrow "
                    "modules such as 'Mathlib.Data.Nat.Basic'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of module names to return.",
                            "default": 50,
                        }
                    },
                },
                _execute=self._list_available_modules,
            ),
            FunctionTool(
                name="check_module_exists",
                description=(
                    "Check whether a specific Lean module can be imported in the "
                    "local environment. Returns true if the module exists and false "
                    "otherwise."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "module": {
                            "type": "string",
                            "description": (
                                "The module name to check, e.g. "
                                "'Mathlib.Data.Nat.Basic'."
                            ),
                        }
                    },
                    "required": ["module"],
                },
                _execute=self._check_module_exists,
            ),
        )

    def _list_available_modules(self, arguments: dict[str, Any]) -> str:
        limit = int(arguments.get("limit", 50))
        modules = self._discover_modules()
        modules = modules[:limit]
        return json.dumps(
            {
                "modules": modules,
                "project_root": str(self.project_root) if self.project_root else None,
                "note": (
                    "Use narrow imports from this list. Bare 'import Mathlib' is forbidden."
                ),
            },
            ensure_ascii=False,
        )

    def _check_module_exists(self, arguments: dict[str, Any]) -> str:
        module = arguments.get("module", "")
        if not isinstance(module, str) or not module.strip():
            return json.dumps(
                {"module": module, "exists": False, "error": "Missing module name."},
                ensure_ascii=False,
            )
        exists = self._check_import_compiles(module)
        if exists is None:
            return json.dumps(
                {
                    "module": module,
                    "exists": None,
                    "error": (
                        "Module check timed out; this does not mean the module is missing. "
                        "Retry the check or choose another narrow module."
                    ),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"module": module, "exists": exists},
            ensure_ascii=False,
        )

    def _discover_modules(self) -> list[str]:
        """Return a sorted list of module names that appear to be available."""
        modules: set[str] = {"Init", "Std", "Lean"}
        if self.project_root is None:
            return sorted(modules)

        modules.update(self._project_modules())
        modules.update(self._manifest_packages())
        modules.update(self._lakefile_requires())
        return sorted(modules)

    def _project_modules(self) -> set[str]:
        """Discover module names from .lean files under the project root."""
        modules: set[str] = set()
        if self.project_root is None:
            return modules
        for path in self.project_root.rglob("*.lean"):
            if ".lake" in path.parts:
                continue
            rel = path.relative_to(self.project_root)
            module = ".".join(rel.with_suffix("").parts)
            if module:
                modules.add(module)
        return modules

    def _manifest_packages(self) -> set[str]:
        """Read package names from lake-manifest.json."""
        packages: set[str] = set()
        if self.project_root is None:
            return packages
        manifest_path = self.project_root / "lake-manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return packages
        for package in data.get("packages", []) if isinstance(data, dict) else ():
            name = package.get("name") if isinstance(package, dict) else None
            if isinstance(name, str):
                packages.add(name)
        return packages

    def _lakefile_requires(self) -> set[str]:
        """Naively extract package names from require statements in lakefile."""
        packages: set[str] = set()
        if self.project_root is None:
            return packages
        for filename in ("lakefile.lean", "lakefile.toml"):
            path = self.project_root / filename
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            # Match both Lean and TOML style require declarations.
            for pattern in (
                re.compile(r"\brequire\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"),
                re.compile(r"\[require\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"),
            ):
                for match in pattern.finditer(text):
                    packages.add(match.group("name"))
        return packages

    def _check_import_compiles(self, module: str) -> bool | None:
        """Verify a module by compiling a temporary file containing only its import.

        Returns ``True`` when the import resolves cleanly, ``False`` when Lean
        reports the package/module as unknown, and ``None`` when the check could
        not be performed (missing executable, timeout, or ambiguous build
        failure). ``None`` prevents the agent from rejecting imports that might
        actually be available.
        """
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(f"import {module}\n")
            tmp_path = Path(tmp.name)

        command: list[str] | None = None
        cwd: str | Path | None = None
        if self.project_root and self._project.is_lake_project:
            command = [self.lake_executable or "lake", "env", "lean", str(tmp_path)]
            cwd = self.project_root
        elif self.lean_executable:
            command = [self.lean_executable, str(tmp_path)]

        try:
            if command is None:
                return None
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.import_check_timeout_seconds,
                check=False,
            )
            if result.returncode == 0:
                return True
            combined = "\n".join(
                part for part in (result.stdout, result.stderr) if part
            )
            if extract_missing_imports(combined):
                return False
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "Lean module check timed out: module=%s timeout=%s",
                module,
                self.import_check_timeout_seconds,
            )
            return None
        except OSError as exc:
            logger.warning(
                "Lean module check could not run: module=%s error=%s",
                module,
                exc,
            )
            return None
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _has_lake_project(self) -> bool:
        return self._project.is_lake_project


class LeanProofToolProvider:
    """Provide a bounded scratch checker for proof-generation tool loops."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        lake_executable: str | None = None,
        lean_executable: str | None = None,
        timeout_seconds: float = 60.0,
        max_source_chars: int = 20_000,
        max_output_chars: int = 12_000,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.lake_executable = resolve_executable(lake_executable, "lake")
        self.lean_executable = resolve_executable(lean_executable, "lean")
        self.timeout_seconds = timeout_seconds
        self.max_source_chars = max_source_chars
        self.max_output_chars = max_output_chars
        self._project = LakeProject(self.project_root)

    def tools(self) -> tuple[Tool, ...]:
        return (
            FunctionTool(
                name="check_lean_snippet",
                description=(
                    "Compile a temporary Lean source file in the current Lake project. "
                    "Use this for #check queries or small proof experiments before returning "
                    "the final proof body. Include all needed import lines in `code`. Never "
                    "copy #check, #print, #eval, #reduce, or import commands into the final answer."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Complete temporary Lean source, including narrow imports.",
                        }
                    },
                    "required": ["code"],
                },
                _execute=self._check_snippet,
            ),
        )

    def _check_snippet(self, arguments: dict[str, Any]) -> str:
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return json.dumps({"ok": False, "error": "Missing non-empty `code`."})
        if len(code) > self.max_source_chars:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"Snippet is too large ({len(code)} chars); "
                        f"limit is {self.max_source_chars}."
                    ),
                }
            )

        command_prefix: list[str] | None = None
        if self.lake_executable and self._project.is_lake_project:
            command_prefix = [self.lake_executable, "env", "lean"]
        elif self.lean_executable:
            command_prefix = [self.lean_executable]
        if command_prefix is None:
            return json.dumps({"ok": False, "error": "Lean checker is unavailable."})

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            prefix="proof_tool_",
            dir=self.project_root,
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(code)
            tmp_path = Path(tmp.name)

        try:
            completed = subprocess.run(
                [*command_prefix, str(tmp_path)],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
            output = "\n".join(
                part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
            )
            truncated = len(output) > self.max_output_chars
            if truncated:
                output = output[: self.max_output_chars] + "\n...[output truncated]"
            return json.dumps(
                {
                    "ok": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "output": output,
                    "truncated": truncated,
                },
                ensure_ascii=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(
                str(part).strip() for part in (exc.stdout, exc.stderr) if part
            )
            return json.dumps(
                {
                    "ok": False,
                    "error": f"Lean snippet check timed out after {self.timeout_seconds}s.",
                    "output": output[: self.max_output_chars],
                },
                ensure_ascii=False,
            )
        except OSError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _has_lake_project(self) -> bool:
        return self._project.is_lake_project


def extract_tool_calls(message: Mapping[str, Any]) -> tuple[ToolCall, ...]:
    """Extract OpenAI-style tool_calls from an assistant message."""
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return ()

    calls: list[ToolCall] = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        arguments_str = function.get("arguments", "{}")
        arguments: dict[str, Any] = {}
        if isinstance(arguments_str, str):
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                arguments = {"raw": arguments_str}
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)


def extract_missing_imports(raw_output: str) -> tuple[str, ...]:
    """Return module/package names that Lean reported as missing.

    This is useful when feeding validation failures back to a formalizer: it can
    be told explicitly which generated imports do not exist locally.
    """
    missing: list[str] = []
    for pattern in _MISSING_IMPORT_RES:
        for match in pattern.finditer(raw_output):
            name = match.group("name")
            if not name:
                continue
            name = name.strip("'\"")
            if name and name not in missing:
                missing.append(name)
    return tuple(missing)


def run_tool_loop(
    transport: ChatTransport,
    config: ChatConfig,
    messages: list[dict[str, Any]],
    tools: Sequence[Tool],
    max_rounds: int,
    execute_tool: Callable[[ToolCall], ToolResult],
    *,
    base_payload: Mapping[str, Any],
    final_n: int = 1,
) -> Mapping[str, Any]:
    """Run a chat completion, allowing the model to call tools first.

    Tool-call rounds use ``n=1`` so that the single stream of tool messages is
    well defined. A tool-capable response that already contains a final answer
    is returned directly when ``final_n == 1``. A separate tool-free request is
    only needed for multiple candidates, when the tool-capable response has no
    usable content, or when the tool budget is exhausted. At the budget limit,
    tools are removed and the model is forced to provide a final answer.
    """
    if not tools:
        payload = dict(base_payload)
        payload["n"] = final_n
        return transport.post_json(
            chat_completions_url(config.base_url),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=config.timeout_seconds,
        )

    tool_rounds = 0
    seen_tool_calls: set[tuple[str, str]] = set()
    while tool_rounds < max_rounds:
        payload = dict(base_payload)
        payload["n"] = 1
        payload["tools"] = [tool.openai_schema() for tool in tools]
        payload["tool_choice"] = "auto"
        response = transport.post_json(
            chat_completions_url(config.base_url),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=config.timeout_seconds,
        )
        message = _first_message(response)
        calls = extract_tool_calls(message)
        if not calls:
            content = message.get("content")
            if final_n == 1 and isinstance(content, str) and content.strip():
                return response
            break
        tool_rounds += 1
        logger.info(
            "Executing model tool calls: round=%d/%d calls=%d",
            tool_rounds,
            max_rounds,
            len(calls),
        )
        messages.append(dict(message))
        for call in calls:
            call_key = (
                call.name,
                json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str),
            )
            if call_key in seen_tool_calls:
                logger.warning(
                    "Skipping duplicate model tool call: round=%d/%d tool=%s",
                    tool_rounds,
                    max_rounds,
                    call.name,
                )
                result = ToolResult(
                    call_id=call.id,
                    content=json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "Duplicate tool call skipped. Use the previous result and "
                                "produce the final answer."
                            ),
                        }
                    ),
                )
            else:
                seen_tool_calls.add(call_key)
                result = execute_tool(call)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )

    if tool_rounds >= max_rounds:
        logger.info(
            "Tool-call budget exhausted; requesting tool-free final answer: rounds=%d",
            tool_rounds,
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "The Lean tool budget is exhausted. Do not call tools again. Return only "
                    "the final proof body that replaces the proof marker, with no markdown, "
                    "imports, #check, #print, #eval, or #reduce commands."
                ),
            }
        )

    final_payload = dict(base_payload)
    final_payload["n"] = final_n
    return transport.post_json(
        chat_completions_url(config.base_url),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        payload=final_payload,
        timeout_seconds=config.timeout_seconds,
    )


def _first_message(response: Mapping[str, Any]) -> dict[str, Any]:
    """Return the first assistant message from a chat-completions response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelAdapterError("Model response is missing a choices list.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ModelAdapterError("Model choice is not an object.")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise ModelAdapterError("Model choice is missing a message.")
    return dict(message)
