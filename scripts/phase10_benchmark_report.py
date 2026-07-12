"""Render Phase 10 traces; legacy pipeline-smoke traces are excluded by construction."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.benchmark_harness import report_suite
from scripts.phase10_benchmark_run import PHASE10_SUITE


def main(argv: list[str] | None = None) -> int:
    return report_suite(PHASE10_SUITE, sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
