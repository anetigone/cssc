"""Bounded scratch checker for proof-generation tool loops."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ...proof_system.lean_project import LakeProject
from ...utils import resolve_executable
from .base import FunctionTool, Tool


logger = logging.getLogger(__name__)


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
