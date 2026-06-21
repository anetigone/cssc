"""Tools that inspect the local Lean / Lake environment."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ...proof_system.lean_project import LakeProject
from ...utils import resolve_executable
from .base import FunctionTool, Tool


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
