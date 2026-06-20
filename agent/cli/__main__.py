"""Run the staged CLI with ``python -m agent.cli``."""

from __future__ import annotations

from .app import main


if __name__ == "__main__":
    raise SystemExit(main())
