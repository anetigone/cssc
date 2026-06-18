"""Environment introspection tools for formalization agents.

These tools give a formalizer agent access to the local Lean environment so it
can avoid generating imports for packages or modules that do not exist locally.
The design is intentionally narrow: only the formalizer needs to know about
imports; proof generators work with an already-imported task template.
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

from ..utils import resolve_executable
from .openai import (
    ChatTransport,
    ModelAdapterError,
    OpenAIChatConfig,
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
        if self.project_root and self._has_lake_project():
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
        if self.project_root is None:
            return False
        return (self.project_root / "lakefile.lean").exists() or (
            self.project_root / "lakefile.toml"
        ).exists()


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
    config: OpenAIChatConfig,
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
    well defined. When the model returns a message without tool calls, a final
    request with ``n=final_n`` and no tools is issued so callers can obtain
    multiple candidates. Raises ``ModelAdapterError`` if the model keeps
    emitting tool calls after ``max_rounds`` rounds.
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
    while True:
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
            break
        if tool_rounds >= max_rounds:
            raise ModelAdapterError(
                f"Tool-call loop exceeded {max_rounds} round(s) without producing a final answer."
            )
        tool_rounds += 1
        messages.append(dict(message))
        for call in calls:
            result = execute_tool(call)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
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
