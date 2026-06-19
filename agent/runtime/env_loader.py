"""Small .env loader for local smoke runs."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Load KEY=value pairs from a .env file into os.environ.

    The parser supports the common dotenv subset: comments, blank lines, an
    optional ``export`` prefix, and single- or double-quoted values with
    backslash escapes handled the way shells and python-dotenv handle them.
    Existing environment variables win unless ``override=True``.

    Only the value side is shell-parsed; the key must be a bare identifier.
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
        # Keys must be bare identifiers; reject quoting/shell metacharacters there.
        if not key or any(ch in key for ch in "\"' \t"):
            raise ValueError(f"Invalid .env line {line_number}: bad key {key!r}")

        value = _parse_value(value)

        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


def _parse_value(value: str) -> str:
    """Parse the value side of a KEY=value line.

    Semantics (matching common dotenv loaders):

    - A leading ``"`` or ``'`` quotes the value; the matching close quote ends
      it. Inside double quotes, backslash escapes are honored (``\\n``, ``\\"``,
      ``\\\\``); inside single quotes the content is literal.
    - An unquoted value runs to end-of-line, but a ``#`` preceded by whitespace
      starts an inline comment (``a # b`` -> ``a``). A ``#`` glued to other
      characters (``http://x/p#frag``) is part of the value.
    - Trailing whitespace on the parsed value is stripped.
    """
    stripped = value.strip()
    if not stripped:
        return ""
    quote = stripped[0]
    if quote in {"'", '"'}:
        close = _find_close_quote(stripped, quote)
        if close == -1:
            # Unterminated quote: fall back to the trimmed raw value.
            return stripped
        inner = stripped[1:close]
        if quote == '"':
            inner = _decode_double_quote_escapes(inner)
        return inner
    # Unquoted: strip an inline comment only when ``#`` follows whitespace.
    comment_at = _inline_comment_index(stripped)
    if comment_at != -1:
        stripped = stripped[:comment_at]
    return stripped.rstrip()


def _find_close_quote(value: str, quote: str) -> int:
    """Return the index of the closing quote, honoring ``\\`` escapes in ``"``."""
    index = 1
    while index < len(value):
        ch = value[index]
        if quote == '"' and ch == "\\" and index + 1 < len(value):
            index += 2
            continue
        if ch == quote:
            return index
        index += 1
    return -1


def _decode_double_quote_escapes(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


def _inline_comment_index(value: str) -> int:
    """Index where a whitespace-preceded ``#`` inline comment starts, else -1."""
    for index in range(1, len(value)):
        if value[index] == "#" and value[index - 1] in {" ", "\t"}:
            return index
    return -1
