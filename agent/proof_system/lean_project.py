"""Shared Lake project detection utilities for Lean tooling."""

from __future__ import annotations

from pathlib import Path


class LakeProject:
    """Detect and query a Lake (Lean package manager) project layout."""

    def __init__(self, project_root: str | Path | None) -> None:
        self.project_root = Path(project_root).resolve() if project_root else None

    @property
    def is_lake_project(self) -> bool:
        """Return True if the root contains a lakefile."""
        if self.project_root is None:
            return False
        return (
            self.project_root / "lakefile.lean"
        ).exists() or (self.project_root / "lakefile.toml").exists()

    def lakefile(self) -> Path | None:
        """Return the detected lakefile path, or None if not a Lake project."""
        if self.project_root is None:
            return None
        for name in ("lakefile.lean", "lakefile.toml"):
            candidate = self.project_root / name
            if candidate.exists():
                return candidate
        return None
