"""Small shared utilities used across agent packages."""

from __future__ import annotations

import shutil
from pathlib import Path


def resolve_executable(explicit: str | None, fallback_name: str) -> str | None:
    """Return an absolute path for an executable.

    If ``explicit`` is provided, try to resolve it through ``shutil.which``.
    Fall back to the literal path if it exists. Otherwise look up
    ``fallback_name`` on ``PATH``.
    """
    if explicit is not None:
        return shutil.which(explicit) or (explicit if Path(explicit).exists() else None)
    return shutil.which(fallback_name)
