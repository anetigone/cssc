"""Backward-compatible entry point for the end-to-end solve command."""

from __future__ import annotations

import sys

from agent.cli.app import main


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"solve", "formalize", "prove", "-h", "--help"}:
        argv = ["solve", *argv]
    raise SystemExit(main(argv))
