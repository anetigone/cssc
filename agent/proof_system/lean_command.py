"""Build Lean / Lake command lines for checking and server modes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import resolve_executable
from .lean_project import LakeProject


class LeanCommandBuilder:
    """Construct `lake env lean` or standalone `lean` command lines."""

    def __init__(
        self,
        project: LakeProject,
        *,
        prefer_lake: bool = True,
        lean_executable: str | None = None,
        lake_executable: str | None = None,
    ) -> None:
        self.project = project
        self.prefer_lake = prefer_lake
        self.lean_executable = resolve_executable(lean_executable, "lean")
        self.lake_executable = resolve_executable(lake_executable, "lake")

    def _lake_check_command(self) -> list[str] | None:
        if self.lake_executable and self.project.is_lake_project:
            return [self.lake_executable, "env", "lean"]
        return None

    def build_check_command(self, candidate_file: Path) -> list[str] | None:
        """Return the command to check ``candidate_file``, or None if no tool."""
        candidate_file = Path(candidate_file)
        if self.prefer_lake:
            lake = self._lake_check_command()
            if lake:
                return [*lake, str(candidate_file)]
        if self.lean_executable:
            return [self.lean_executable, str(candidate_file)]
        lake = self._lake_check_command()
        if lake:
            return [*lake, str(candidate_file)]
        return None

    def build_server_command(self) -> list[str] | None:
        """Return the command to start ``lean --server``, or None."""
        if self.prefer_lake:
            lake = self._lake_check_command()
            if lake:
                return [*lake, "--server"]
        if self.lean_executable:
            return [self.lean_executable, "--server"]
        lake = self._lake_check_command()
        if lake:
            return [*lake, "--server"]
        return None

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"project={self.project!r}, "
            f"prefer_lake={self.prefer_lake}, "
            f"lean_executable={self.lean_executable!r}, "
            f"lake_executable={self.lake_executable!r})"
        )
