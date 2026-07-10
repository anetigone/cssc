"""Run the Phase 8.5 benchmark for one (task, arm, repetition).

Stage 0 skeleton (``tmp/phase8_5_benchmark_plan.md`` §9). It supports:

- **live track**: subprocess into ``python -m agent.cli.app prove`` with the
  arm's execution mode + frontier policy and a budget derived from the task's
  ``budget_profile``. This is the real run path — it consumes model tokens and
  needs a real Lean toolchain, so it is run by hand, not in CI.
- **controlled track**: loads the task's scenario JSON, deserializes its
  proposals through ``structured_action_proposal_from_dict`` (validates the
  scenario is well-formed), and stops. Stage 0 does NOT replay the scenario
  through a scripted checker — that arrives in Stage 2.
- ``--dry-run``: writes a minimal ``run_summary`` stub trace so the report
  script's aggregation path can be exercised without any model or Lean.

Output trace path: ``<runs-root>/<suite>/<arm>/<task>/<rep>.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.search.structured.proposal.core import (  # noqa: E402
    structured_action_proposal_from_dict,
)

# arm -> (execution_mode, frontier_policy)
ARM_TABLE: dict[str, tuple[str, str]] = {
    "A0": ("minimal", "legacy"),  # frontier_policy ignored by minimal
    "A1": ("structured", "legacy"),
    "A2": ("structured", "cost_aware_v1"),
    "A3": ("structured", "cost_aware_v2"),
    "A4": ("structured", "value_per_cost_v1"),
}

# budget_profile -> (max_model_calls, max_checks)
BUDGET_TABLE: dict[str, tuple[int, int]] = {
    "short": (4, 6),
    "repair": (8, 10),
    "multi_obligation": (16, 20),
}


def _load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _find_task(manifest: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    for row in manifest:
        if row["task_id"] == task_id:
            return row
    raise KeyError(f"task_id {task_id!r} not in manifest")


def _trace_path(
    runs_root: Path, suite: str, arm: str, task_id: str, rep: int
) -> Path:
    return runs_root / suite / arm / task_id / f"{rep}.jsonl"


def _dry_run_stub(
    row: dict[str, Any], arm: str, execution_mode: str, frontier_policy: str
) -> dict[str, Any]:
    """A minimal run_summary event the report script can aggregate.

    Real runs are produced by the CLI; this stub only exercises the report
    pipeline (dry-run path, no model / no Lean).
    """
    terminal = row["expected_terminal"]
    accepted = terminal == "accepted"
    return {
        "event": "run_summary",
        "run_id": f"{arm}-{row['task_id']}-dryrun",
        "task": {
            "task_id": row["task_id"],
            "hole_marker": "{{proof}}",
            "imports": [],
            "metadata": {},
        },
        "accepted": accepted,
        "stop_reason": "dry_run_stub" if not accepted else "accepted",
        "attempt_count": 0,
        "accepted_attempt_index": None,
        "budget": {
            "checks_used": 0,
            "model_calls_used": 0,
            "elapsed_seconds": 0.0,
            "remaining_checks": 0,
            "remaining_model_calls": 0,
            "exhausted_reason": None,
        },
        "metrics": {
            "sample_id": f"{arm}-{row['task_id']}",
            "task_id": row["task_id"],
            "accepted": accepted,
            "execution_mode": execution_mode,
            "stop_reason": "dry_run_stub" if not accepted else "accepted",
            "attempt_count": 0,
            "budget_checks_used": 0,
            "budget_model_calls_used": 0,
            "budget_exhausted_reason": None,
            "model_input_tokens": 0,
            "model_output_tokens": 0,
            "attempts": [],
        },
        "metadata": {
            "frontier_policy": frontier_policy,
            "result_summary": {
                "workspace_status": terminal,
                "accepted_obligations": [],
                "open_obligations": [],
                "blocked_obligations": [],
            },
        },
    }


def _run_live(
    row: dict[str, Any],
    arm: str,
    execution_mode: str,
    frontier_policy: str,
    *,
    project_root: Path,
    fixture_abs: Path,
    lean_timeout: float,
    out: Path,
) -> int:
    max_calls, max_checks = BUDGET_TABLE[row["budget_profile"]]
    cmd = [
        sys.executable,
        "-m",
        "agent.cli.app",
        "prove",
        str(fixture_abs),
        "--project-root",
        str(project_root),
        "--execution-mode",
        execution_mode,
        "--frontier-policy",
        frontier_policy,
        "--max-model-calls",
        str(max_calls),
        "--max-checks",
        str(max_checks),
        "--lean-timeout",
        str(lean_timeout),
        "--trace-jsonl",
        str(out),
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(cmd, cwd=str(ROOT))
    return completed.returncode


def _run_controlled(
    row: dict[str, Any], fixtures_dir: Path
) -> dict[str, Any]:
    """Stage 0 placeholder: load + deserialize the scenario, do not replay."""
    scenario_path = fixtures_dir / row["controlled_scenario"]
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    proposals = scenario["proposals"]
    deserialized = 0
    for proposal in proposals:
        structured_action_proposal_from_dict(proposal)
        deserialized += 1
    return {
        "track": "controlled",
        "task_id": row["task_id"],
        "proposals_loaded": deserialized,
        "note": "Stage 0 placeholder: scenario deserialized; replay arrives in Stage 2.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="tests/fixtures/phase8_benchmark/manifest.jsonl",
    )
    parser.add_argument("--task", help="task_id; omit to run all canary tasks")
    parser.add_argument("--arm", choices=list(ARM_TABLE), default="A0")
    parser.add_argument("--track", choices=("live", "controlled"), default="live")
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--suite-version", default="stage0-canary")
    parser.add_argument("--runs-root", default=".runs/phase8")
    parser.add_argument("--project-root", default="lean_workspace")
    parser.add_argument("--fixtures-dir", default="tests/fixtures/phase8_benchmark")
    parser.add_argument("--lean-timeout", type=float, default=30.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write a stub trace; do not call the CLI (no model, no Lean).",
    )
    parser.add_argument(
        "--from-trace",
        default=None,
        help="dry-run only: copy this existing trace as the run output.",
    )
    args = parser.parse_args()

    manifest_path = (ROOT / args.manifest).resolve()
    fixtures_dir = (ROOT / args.fixtures_dir).resolve()
    project_root = (ROOT / args.project_root).resolve()
    runs_root = (ROOT / args.runs_root).resolve()

    try:
        manifest = _load_manifest(manifest_path)
    except FileNotFoundError:
        print(json.dumps({"ok": False, "error": f"manifest not found: {manifest_path}"}))
        return 2

    targets = (
        [_find_task(manifest, args.task)] if args.task else list(manifest)
    )

    execution_mode, frontier_policy = ARM_TABLE[args.arm]
    results = []
    for row in targets:
        task_id = row["task_id"]
        out = _trace_path(
            runs_root, args.suite_version, args.arm, task_id, args.repetition
        )

        if args.track == "controlled":
            results.append(_run_controlled(row, fixtures_dir))
            continue

        if args.dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
            if args.from_trace:
                src = (ROOT / args.from_trace).resolve()
                shutil.copyfile(src, out)
                payload = {"ok": True, "track": "live", "dry_run": True, "copied_from": str(src)}
            else:
                stub = _dry_run_stub(row, args.arm, execution_mode, frontier_policy)
                with out.open("w", encoding="utf-8") as handle:
                    handle.write(json.dumps(stub) + "\n")
                payload = {"ok": True, "track": "live", "dry_run": True}
            payload.update(
                {"task": task_id, "arm": args.arm, "trace_jsonl": str(out.relative_to(ROOT))}
            )
            results.append(payload)
            continue

        # Real live run: consumes model tokens, needs a real Lean toolchain.
        fixture_abs = fixtures_dir / row["source"]
        rc = _run_live(
            row,
            args.arm,
            execution_mode,
            frontier_policy,
            project_root=project_root,
            fixture_abs=fixture_abs,
            lean_timeout=args.lean_timeout,
            out=out,
        )
        results.append(
            {
                "ok": rc == 0,
                "track": "live",
                "task": task_id,
                "arm": args.arm,
                "trace_jsonl": str(out.relative_to(ROOT)),
                "cli_returncode": rc,
            }
        )

    print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
