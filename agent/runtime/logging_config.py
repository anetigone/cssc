"""Logging configuration helpers for CLI entry points."""

from __future__ import annotations

import logging
from pathlib import Path


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(
    *,
    level: str = "WARNING",
    log_file: str | None = None,
) -> None:
    """Configure process-wide logging for command-line runs."""

    numeric_level = _level_value(level)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_path = Path(log_file)
        if log_path.parent != Path("."):
            log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=numeric_level,
        format=DEFAULT_LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def _level_value(level: str) -> int:
    value = getattr(logging, level.upper(), None)
    if not isinstance(value, int):
        choices = ", ".join(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
        raise ValueError(f"Unknown log level {level!r}. Expected one of: {choices}.")
    return value
