"""Run the Phase 10 controlled or live benchmark suite."""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_harness import BenchmarkSuiteConfig, build_replay_controller, run_suite

PHASE10_ARMS = {
    "C0": ("structured", "legacy"),
    "C1": ("structured", "action_cost_aware_v1"),
    "C2": ("structured", "action_cost_aware_v1"),
    "C3": ("structured", "action_cost_aware_v1"),
    "C4": ("structured", "action_cost_aware_v1"),
    "A0": ("minimal", "legacy"),
    "A1": ("structured", "legacy"),
    "A2": ("structured", "action_cost_aware_v1"),
    "A3": ("structured", "action_cost_aware_v1"),
    "A4": ("structured", "action_cost_aware_v1"),
    "A5": ("structured", "action_cost_aware_v1"),
    "A6": ("structured", "action_cost_aware_v1"),
}

PHASE10_ARM_FEATURES = {
    "C0": {"frontier": "branch", "cost_source": "none", "remaining_budget": False, "model_mode": "none"},
    "C1": {"frontier": "action", "cost_source": "static", "remaining_budget": False, "model_mode": "none"},
    "C2": {"frontier": "action", "cost_source": "empirical", "remaining_budget": False, "model_mode": "none"},
    "C3": {"frontier": "action", "cost_source": "empirical", "remaining_budget": True, "model_mode": "none"},
    "C4": {"frontier": "action", "cost_source": "empirical", "remaining_budget": True, "model_mode": "routed"},
    "A0": {"frontier": "minimal", "cost_source": "none", "remaining_budget": False, "model_mode": "single_strong"},
    "A1": {"frontier": "branch", "cost_source": "none", "remaining_budget": False, "model_mode": "single_strong"},
    "A2": {"frontier": "action", "cost_source": "static", "remaining_budget": False, "model_mode": "single_strong"},
    "A3": {"frontier": "action", "cost_source": "empirical", "remaining_budget": False, "model_mode": "single_strong"},
    "A4": {"frontier": "action", "cost_source": "empirical", "remaining_budget": True, "model_mode": "single_strong"},
    "A5": {"frontier": "action", "cost_source": "empirical", "remaining_budget": True, "model_mode": "routed"},
    "A6": {"frontier": "action", "cost_source": "empirical", "remaining_budget": True, "model_mode": "single_cheap"},
}

# Phase 10 is an evaluation phase, so an arm is runnable only when every
# advertised dimension reaches the controller.  The current action runtime
# exposes static-cost + remaining-budget admission as one policy; replay also
# has no model calls on which cheap/strong routing could act.  Keep the planned
# arm names visible, but refuse to manufacture indistinguishable observations.
PHASE10_CONTROLLED_ARM_BLOCKS = {
    "C2": "the controller has no independent frozen-empirical-cost switch",
    "C3": "the controller has no independent remaining-budget-policy switch",
    "C4": "controlled replay has no model calls, so Phase 9.4 routing cannot execute",
}

PHASE10_SUITE = BenchmarkSuiteConfig(
    name="Phase 10",
    manifest="tests/fixtures/phase10_benchmark/manifest.jsonl",
    fixtures_dir="tests/fixtures/phase10_benchmark",
    runs_root=".runs/phase10",
    suite_version="phase10-canary-v1",
    arms=PHASE10_ARMS,
    default_arm="C0",
    report_title="Phase 10 benchmark report",
    report_footer=(
        "Controlled simulated costs are not billed costs. Savings require "
        "paired, fully measured live runs."
    ),
    routed_arms=frozenset({"C4", "A5"}),
    single_cheap_arms=frozenset({"A6"}),
    arm_features=PHASE10_ARM_FEATURES,
    controlled_arm_blocks=PHASE10_CONTROLLED_ARM_BLOCKS,
)


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
    return build_replay_controller(scenario=normalized, **kwargs)


def main(argv: list[str] | None = None) -> int:
    return run_suite(
        PHASE10_SUITE,
        sys.argv[1:] if argv is None else argv,
        replay_builder=_phase10_replay_controller,
    )


if __name__ == "__main__":
    raise SystemExit(main())
