"""Render Phase 10 traces; legacy pipeline-smoke traces are excluded by construction."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import phase8_benchmark_report as base

_base_render = base._render


def _phase10_render(rows):
    return _base_render(rows).replace("Phase 8.5 benchmark report", "Phase 10 benchmark report").replace(
        "_Stage 0 skeleton: raw per-run rows only. Savings, CIs and verdicts arrive in Stage 4._",
        "_Controlled simulated costs are not billed costs. Savings require paired, fully measured live runs._",
    )


base._render = _phase10_render


def main(argv: list[str] | None = None) -> int:
    defaults = ["--runs-dir", ".runs/phase10", "--manifest", "tests/fixtures/phase10_benchmark/manifest.jsonl"]
    return base.main([*defaults, *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
