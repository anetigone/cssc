"""Validate the frozen Phase 10 benchmark suite without spending model tokens."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase8_benchmark_validate import validate as validate_base  # noqa: E402

SUITE_VERSION = "phase10-canary-v1"
ARMS = {"C0", "C1", "C2", "C3", "C4"}
LEAK_PATTERNS = {
    "tactic/proof hint": re.compile(r"\b(?:by|rfl|simp|omega|aesop|linarith|trivial|exact|apply)\b", re.I),
    "benchmark expectation": re.compile(r"expected[_ -]?(?:terminal|action|attempt)|controlled|oracle|canary", re.I),
    "helper structure": re.compile(r"helper\s*\d*|action[_ -]?kind|capability|probe[_ -]?signature", re.I),
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def hardening_errors(rows: list[dict[str, Any]], fixtures: Path) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    layers: dict[str, int] = {f"L{i}": 0 for i in range(1, 7)}
    policy_dependent = 0
    competition = 0
    for row in rows:
        task = str(row.get("task_id", "<missing>"))
        if task in ids:
            errors.append(f"[{task}] duplicate task_id")
        ids.add(task)
        layer = str(row.get("layer"))
        if layer in layers:
            layers[layer] += 1
        if row.get("suite_version") != SUITE_VERSION:
            errors.append(f"[{task}] suite_version must be {SUITE_VERSION!r}")
        for key in ("controlled_expectation", "live_expectation"):
            if not isinstance(row.get(key), dict):
                errors.append(f"[{task}] {key} must be an object")
        source = fixtures / str(row.get("source", ""))
        if source.is_file():
            text = source.read_text(encoding="utf-8")
            comment_matches = re.findall(r"/-(.*?)-/|--([^\n]*)", text, re.S)
            comments = "\n".join(block or line for block, line in comment_matches)
            for label, pattern in LEAK_PATTERNS.items():
                if pattern.search(comments):
                    errors.append(f"[{task}] prompt contamination ({label}) in {source.name}")
        scenario_path = fixtures / str(row.get("controlled_scenario", ""))
        if not scenario_path.is_file():
            continue
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        oracle = scenario.get("oracle")
        if not isinstance(oracle, dict) or not isinstance(oracle.get("lowest_attained_cost"), dict):
            errors.append(f"[{task}] scenario requires oracle.lowest_attained_cost")
        events = scenario.get("selection_events", [])
        if any(len(event.get("eligible_action_ids", [])) >= 2 for event in events):
            competition += 1
        orders = scenario.get("expected_action_order_by_arm", {})
        if len({tuple(v) for v in orders.values() if isinstance(v, list)}) >= 2:
            policy_dependent += 1
        costs = scenario.get("simulated_costs", [])
        if not costs or any(item.get("measurement") != "simulated" for item in costs):
            errors.append(f"[{task}] all controlled costs must be explicitly simulated")
    if len(rows) == 6:
        if any(count != 1 for count in layers.values()):
            errors.append(f"[suite] canary must contain exactly one task per layer: {layers}")
        if policy_dependent < 4:
            errors.append(f"[suite] policy-dependent action order only {policy_dependent}/6; need >=4")
        if competition < 3:
            errors.append(f"[suite] too few canaries expose multi-action selection: {competition}/6")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="tests/fixtures/phase10_benchmark/manifest.jsonl")
    parser.add_argument("--fixtures-dir", default="tests/fixtures/phase10_benchmark")
    parser.add_argument("--skip-lean-smoke", action="store_true")
    parser.add_argument("--lean-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    manifest = (ROOT / args.manifest).resolve()
    fixtures = (ROOT / args.fixtures_dir).resolve()
    try:
        rows = load_rows(manifest)
        errors = hardening_errors(rows, fixtures)
        base = validate_base(manifest, fixtures, lean_timeout=args.lean_timeout, skip_lean_smoke=args.skip_lean_smoke)
        errors.extend(base.errors)
        payload = {"ok": not errors, "suite_version": SUITE_VERSION, "checked_tasks": base.checked, "errors": errors, "warnings": base.warnings}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        payload = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
