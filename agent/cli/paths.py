"""Path resolution helpers for the CLI."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def resolve_agent_root(value: str | Path | None) -> Path:
    return Path(value or ROOT).resolve()


def resolve_agent_path(agent_root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (agent_root / path).resolve()


def find_lake_root(source: str | Path) -> Path | None:
    path = Path(source).resolve()
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / "lakefile.lean").exists() or (candidate / "lakefile.toml").exists():
            return candidate
    return None
