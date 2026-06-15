"""Small .env loader for local smoke runs."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Load KEY=value pairs from a .env file into os.environ.

    The parser intentionally supports the common subset used by local config:
    comments, blank lines, optional `export`, and single- or double-quoted
    values. Existing environment variables win unless override=True.
    """

    env_path = Path(path)
    if not env_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=value")

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid .env line {line_number}: empty key")
        value = _strip_inline_comment(value.strip())
        value = _unquote(value)

        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


def _strip_inline_comment(value: str) -> str:
    if not value or value[0] in {"'", '"'}:
        return value
    marker = value.find(" #")
    return value[:marker].rstrip() if marker != -1 else value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
