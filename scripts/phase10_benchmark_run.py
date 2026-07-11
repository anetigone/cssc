"""Phase 10 runner. Reuses the frozen Phase 8 execution engine with Phase 10 arms."""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import phase8_benchmark_run as base
from scripts.phase8_benchmark_replay import build_replay_controller as _build_replay_controller

base.ARM_TABLE = {
    "C0": ("structured", "legacy"),
    "C1": ("structured", "cost_aware_v1"),
    "C2": ("structured", "cost_aware_v2"),
    "C3": ("structured", "value_per_cost_v1"),
    "C4": ("structured", "value_per_cost_v1"),
    "A0": ("minimal", "legacy"),
    "A1": ("structured", "legacy"),
    "A2": ("structured", "cost_aware_v1"),
    "A3": ("structured", "cost_aware_v2"),
    "A4": ("structured", "value_per_cost_v1"),
    "A5": ("structured", "value_per_cost_v1"),
    "A6": ("structured", "value_per_cost_v1"),
}


def _phase10_replay_controller(*, scenario, **kwargs):
    """Accept concise Phase 10 oracles by deriving their candidate discriminator."""
    normalized = deepcopy(scenario)
    proofs = [
        str((proposal.get("payload") or {}).get("proof_text", ""))
        for proposal in normalized.get("proposals", [])
    ]
    for index, oracle in enumerate(normalized.get("expected_check_results", [])):
        if not oracle.get("on_candidate_contains") and index < len(proofs):
            oracle["on_candidate_contains"] = proofs[index]
    return _build_replay_controller(scenario=normalized, **kwargs)


base.build_replay_controller = _phase10_replay_controller


def main(argv: list[str] | None = None) -> int:
    defaults = ["--manifest", "tests/fixtures/phase10_benchmark/manifest.jsonl", "--fixtures-dir", "tests/fixtures/phase10_benchmark", "--runs-root", ".runs/phase10"]
    return base.main([*defaults, *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
