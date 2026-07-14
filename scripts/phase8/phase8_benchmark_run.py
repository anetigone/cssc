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
import hashlib
from datetime import datetime, timezone
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.runtime.trace_store import JsonlTraceStore  # noqa: E402
from agent.search.budget import BudgetConfig  # noqa: E402
from agent.tasks.task_builder import LeanTaskBuilder, TaskBuildError  # noqa: E402
from scripts.phase8.phase8_benchmark_replay import build_replay_controller  # noqa: E402

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


def _provenance_path(trace_path: Path) -> Path:
    return trace_path.with_suffix(".meta.json")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_output(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _lean_environment(project_root: Path) -> dict[str, Any]:
    toolchain_path = project_root / "lean-toolchain"
    manifest_path = project_root / "lake-manifest.json"
    toolchain = (
        toolchain_path.read_text(encoding="utf-8").strip()
        if toolchain_path.is_file()
        else None
    )
    mathlib_rev = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for package in manifest.get("packages", []):
                if package.get("name") == "mathlib":
                    mathlib_rev = package.get("rev")
                    break
        except (json.JSONDecodeError, OSError):
            pass
    return {"lean_toolchain": toolchain, "mathlib_rev": mathlib_rev}


def _build_provenance(
    *,
    row: dict[str, Any],
    suite: str,
    arm: str,
    repetition: int,
    execution_mode: str,
    frontier_policy: str,
    project_root: Path,
    trace_path: Path,
    proof_model: str | None,
    proof_temperature: float,
    proof_max_tokens: int,
    model_timeout: float,
    lean_timeout: float,
    dry_run: bool,
    track: str = "live",
) -> dict[str, Any]:
    max_calls, max_checks = BUDGET_TABLE[row["budget_profile"]]
    git_status = _git_output("status", "--porcelain", "--untracked-files=no")
    return {
        "schema_version": 1,
        "status": "started",
        "started_at": _utc_now(),
        "completed_at": None,
        "suite_version": suite,
        "arm": arm,
        "task_id": row["task_id"],
        "repetition": repetition,
        "track": track,
        "dry_run": dry_run,
        "execution_mode": execution_mode,
        "frontier_policy": frontier_policy,
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_dirty_tracked": bool(git_status),
        "proof_model": proof_model,
        "proof_temperature": proof_temperature,
        "proof_max_tokens": proof_max_tokens,
        "model_timeout": model_timeout,
        "lean_timeout": lean_timeout,
        "budget_profile": row["budget_profile"],
        "max_model_calls": max_calls,
        "max_checks": max_checks,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "project_root": str(project_root),
        "trace_jsonl": str(trace_path),
        **_lean_environment(project_root),
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_single_run_summary(trace_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not trace_path.is_file():
        return None, "trace file was not created"
    summaries: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        trace_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON at line {line_number}: {exc.msg}"
        if event.get("event") == "run_summary":
            summaries.append(event)
    if len(summaries) != 1:
        return None, f"expected exactly one run_summary, found {len(summaries)}"
    return summaries[0], None


def _validate_segment(value: str, *, label: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be one non-empty path segment: {value!r}")


def _dry_run_stub(
    row: dict[str, Any], arm: str, execution_mode: str, frontier_policy: str
) -> dict[str, Any]:
    """A minimal run_summary event the report script can aggregate.

    Real runs are produced by the CLI; this stub only exercises the report
    pipeline (dry-run path, no model / no Lean).
    """
    terminal = row["expected_terminal"]
    accepted = terminal == "accepted"
    metadata: dict[str, Any] = {}
    if execution_mode == "structured":
        metadata = {
            "frontier_policy": frontier_policy,
            "result_summary": {
                "workspace_status": terminal,
                "accepted_obligations": [],
                "open_obligations": [],
                "blocked_obligations": [],
            },
        }
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
        "metadata": metadata,
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
    model_timeout: float,
    proof_model: str,
    proof_temperature: float,
    proof_max_tokens: int,
    enable_model_routing: bool,
    strong_proof_model: str | None,
    action_cost_source: str,
    remaining_budget_policy: bool,
    cost_history_snapshot: Path | None,
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
        "--model-timeout",
        str(model_timeout),
        "--proof-model",
        proof_model,
        "--proof-temperature",
        str(proof_temperature),
        "--proof-max-tokens",
        str(proof_max_tokens),
        "--trace-jsonl",
        str(out),
    ]
    if enable_model_routing:
        cmd.extend(["--enable-model-routing", "--strong-proof-model", str(strong_proof_model)])
    cmd.extend(["--action-cost-source", action_cost_source])
    cmd.append(
        "--enable-remaining-budget-policy"
        if remaining_budget_policy
        else "--disable-remaining-budget-policy"
    )
    if cost_history_snapshot is not None:
        cmd.extend(["--cost-history-snapshot", str(cost_history_snapshot)])
    out.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(cmd, cwd=str(ROOT))
    return completed.returncode


def _run_controlled(
    row: dict[str, Any],
    fixtures_dir: Path,
    *,
    arm: str,
    frontier_policy: str,
    suite: str,
    repetition: int,
    runs_root: Path,
    overwrite: bool,
    replay_builder=build_replay_controller,
) -> dict[str, Any]:
    """Drive the real StructuredController with scripted components (Stage 2).

    No model, no Lean: a ``ReplayGenerator`` emits the scenario's proposals and
    a ``ScenarioFakeAdapter`` answers checks from the scenario's oracle. The
    real frontier / reducer / assembly / ResultSummary pipeline runs unchanged,
    so the frontier_policy genuinely affects scheduling. The trace is written by
    the real ``JsonlTraceStore``, identical in shape to a live run, so the report
    script parses it unchanged.
    """
    task_id = row["task_id"]
    scenario_path = fixtures_dir / row["controlled_scenario"]
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    source = (fixtures_dir / row["source"]).read_text(encoding="utf-8")
    try:
        tasks = LeanTaskBuilder().build_from_source(source, source_path=row["source"])
    except TaskBuildError as exc:
        return {
            "ok": False,
            "track": "controlled",
            "task": task_id,
            "arm": arm,
            "error": f"LeanTaskBuilder rejected scaffold: {exc}",
        }
    if len(tasks) != 1:
        return {
            "ok": False,
            "track": "controlled",
            "task": task_id,
            "arm": arm,
            "error": f"expected 1 task from scaffold, got {len(tasks)}",
        }
    task = tasks[0]

    max_calls, max_checks = BUDGET_TABLE[row["budget_profile"]]
    budget_config = BudgetConfig(max_checks=max_checks, max_model_calls=max_calls)

    out = _trace_path(runs_root, suite, arm, task_id, repetition)
    meta = _provenance_path(out)
    if overwrite:
        out.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    provenance = _build_provenance(
        row=row,
        suite=suite,
        arm=arm,
        repetition=repetition,
        execution_mode="structured",
        frontier_policy=frontier_policy,
        project_root=fixtures_dir,  # controlled runs have no Lean project
        trace_path=out,
        proof_model=None,
        proof_temperature=0.0,
        proof_max_tokens=0,
        model_timeout=0.0,
        lean_timeout=0.0,
        dry_run=False,
        track="controlled",
    )
    # controlled runs are deterministic and offline; clear Lean/model env fields
    # that have no meaning without a real toolchain.
    provenance["lean_toolchain"] = None
    provenance["mathlib_rev"] = None
    _write_json_atomic(meta, provenance)

    with tempfile.TemporaryDirectory() as workspace_root:
        controller, _generator, _adapter = replay_builder(
            scenario=scenario,
            frontier_policy=frontier_policy,
            budget_config=budget_config,
            workspace_root=workspace_root,
        )
        result = controller.run(task)

    JsonlTraceStore(out).append_result(result)
    summary, trace_error = _read_single_run_summary(out)
    run_ok = trace_error is None
    provenance.update(
        {
            "status": "completed" if run_ok else "failed",
            "completed_at": _utc_now(),
            "trace_error": trace_error,
            "cli_returncode": None,
        }
    )
    _write_json_atomic(meta, provenance)
    return {
        "ok": run_ok,
        "track": "controlled",
        "task": task_id,
        "arm": arm,
        "trace_jsonl": _display_path(out),
        "proof_accepted": summary.get("accepted") if summary else None,
        "trace_error": trace_error,
    }


def main(
    argv: list[str] | None = None,
    *,
    arm_table: dict[str, tuple[str, str]] | None = None,
    default_manifest: str = "tests/fixtures/phase8_benchmark/manifest.jsonl",
    default_fixtures_dir: str = "tests/fixtures/phase8_benchmark",
    default_runs_root: str = ".runs/phase8",
    default_suite_version: str = "stage0-canary",
    default_arm: str = "A0",
    description: str | None = None,
    replay_builder=build_replay_controller,
    routed_arms: frozenset[str] = frozenset(),
    single_cheap_arms: frozenset[str] = frozenset(),
    arm_features: dict[str, dict[str, object]] | None = None,
    controlled_arm_blocks: dict[str, str] | None = None,
) -> int:
    selected_arms = ARM_TABLE if arm_table is None else arm_table
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument(
        "--manifest",
        default=default_manifest,
    )
    parser.add_argument("--task", help="task_id; omit to run all canary tasks")
    parser.add_argument("--arm", choices=list(selected_arms), default=default_arm)
    parser.add_argument("--track", choices=("live", "controlled"), default="live")
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--suite-version", default=default_suite_version)
    parser.add_argument("--runs-root", default=default_runs_root)
    parser.add_argument("--project-root", default="lean_workspace")
    parser.add_argument("--fixtures-dir", default=default_fixtures_dir)
    parser.add_argument("--lean-timeout", type=float, default=30.0)
    parser.add_argument("--model-timeout", type=float, default=60.0)
    parser.add_argument("--proof-model", default=None)
    parser.add_argument("--strong-proof-model", default=None)
    parser.add_argument("--cost-history-snapshot", default=None)
    parser.add_argument("--proof-temperature", type=float, default=0.2)
    parser.add_argument("--proof-max-tokens", type=int, default=16384)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing trace/provenance pair for the same run tuple.",
    )
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
    args = parser.parse_args(argv)

    try:
        _validate_segment(args.suite_version, label="suite-version")
        if args.repetition < 1:
            raise ValueError("repetition must be >= 1")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    if args.from_trace and not args.dry_run:
        print(json.dumps({"ok": False, "error": "--from-trace requires --dry-run"}))
        return 2
    needs_routing = args.arm in routed_arms
    features = dict((arm_features or {}).get(args.arm, {}))
    action_cost_source = str(features.get("cost_source", "auto"))
    remaining_budget_policy = bool(features.get("remaining_budget", True))
    history_path = (
        (ROOT / args.cost_history_snapshot).resolve()
        if args.cost_history_snapshot
        else None
    )
    if args.track == "live" and not args.dry_run and not args.proof_model:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "live benchmark runs require explicit --proof-model for provenance",
                }
            )
        )
        return 2
    if args.track == "live" and not args.dry_run and needs_routing and not args.strong_proof_model:
        print(json.dumps({"ok": False, "error": f"{args.arm} requires --strong-proof-model for Phase 9.4 routing"}))
        return 2
    if (
        args.track == "live"
        and not args.dry_run
        and action_cost_source == "empirical"
        and history_path is None
    ):
        print(json.dumps({"ok": False, "error": f"{args.arm} requires --cost-history-snapshot"}))
        return 2
    if history_path is not None and not history_path.is_file():
        print(json.dumps({"ok": False, "error": f"cost history snapshot not found: {history_path}"}))
        return 2
    if args.track == "controlled" and selected_arms[args.arm][0] != "structured":
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "controlled track requires a structured arm; "
                    f"{args.arm}/minimal has no frontier policy",
                }
            )
        )
        return 2
    controlled_block = (controlled_arm_blocks or {}).get(args.arm)
    if args.track == "controlled" and controlled_block:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"controlled arm {args.arm} is not executable: {controlled_block}",
                }
            )
        )
        return 2

    manifest_path = (ROOT / args.manifest).resolve()
    fixtures_dir = (ROOT / args.fixtures_dir).resolve()
    project_root = (ROOT / args.project_root).resolve()
    runs_root = (ROOT / args.runs_root).resolve()

    try:
        manifest = _load_manifest(manifest_path)
    except FileNotFoundError:
        print(json.dumps({"ok": False, "error": f"manifest not found: {manifest_path}"}))
        return 2

    try:
        targets = [_find_task(manifest, args.task)] if args.task else list(manifest)
    except KeyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    execution_mode, frontier_policy = selected_arms[args.arm]
    copied_trace: Path | None = None
    if args.track == "live":
        copied_trace = (ROOT / args.from_trace).resolve() if args.from_trace else None
        if copied_trace is not None and not copied_trace.is_file():
            print(
                json.dumps(
                    {"ok": False, "error": f"--from-trace not found: {copied_trace}"}
                )
            )
            return 2
        if copied_trace is not None:
            for row in targets:
                out = _trace_path(
                    runs_root,
                    args.suite_version,
                    args.arm,
                    row["task_id"],
                    args.repetition,
                )
                if copied_trace == out:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "error": "--from-trace must differ from the destination trace",
                            }
                        )
                    )
                    return 2

    # Both tracks write a trace + provenance pair, so both get no-overwrite
    # protection (controlled runs are reproducible but must still not silently
    # clobber an existing tuple).
    collisions: list[str] = []
    if not args.overwrite:
        for row in targets:
            out = _trace_path(
                runs_root,
                args.suite_version,
                args.arm,
                row["task_id"],
                args.repetition,
            )
            meta = _provenance_path(out)
            if out.exists() or meta.exists():
                collisions.append(_display_path(out))
    if collisions:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "run tuple already exists; choose another repetition or pass --overwrite",
                    "collisions": collisions,
                },
                indent=2,
            )
        )
        return 2

    results = []
    for row in targets:
        task_id = row["task_id"]
        out = _trace_path(
            runs_root, args.suite_version, args.arm, task_id, args.repetition
        )

        if args.track == "controlled":
            results.append(
                _run_controlled(
                    row,
                    fixtures_dir,
                    arm=args.arm,
                    frontier_policy=frontier_policy,
                    suite=args.suite_version,
                    repetition=args.repetition,
                    runs_root=runs_root,
                    overwrite=args.overwrite,
                    replay_builder=replay_builder,
                )
            )
            continue

        meta = _provenance_path(out)
        if args.overwrite:
            out.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
        provenance = _build_provenance(
            row=row,
            suite=args.suite_version,
            arm=args.arm,
            repetition=args.repetition,
            execution_mode=execution_mode,
            frontier_policy=frontier_policy,
            project_root=project_root,
            trace_path=out,
            proof_model=args.proof_model,
            proof_temperature=args.proof_temperature,
            proof_max_tokens=args.proof_max_tokens,
            model_timeout=args.model_timeout,
            lean_timeout=args.lean_timeout,
            dry_run=args.dry_run,
        )
        provenance["arm_features"] = features
        provenance["action_cost_source"] = action_cost_source
        provenance["remaining_budget_policy"] = remaining_budget_policy
        provenance["cost_history_snapshot"] = (
            _display_path(history_path) if history_path is not None else None
        )
        provenance["cost_history_snapshot_file_sha256"] = (
            hashlib.sha256(history_path.read_bytes()).hexdigest()
            if history_path is not None else None
        )
        provenance["model_routing_enabled"] = needs_routing
        provenance["strong_proof_model"] = args.strong_proof_model
        _write_json_atomic(meta, provenance)

        if args.dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
            if args.from_trace:
                src = copied_trace
                assert src is not None
                shutil.copyfile(src, out)
                payload = {"ok": True, "track": "live", "dry_run": True, "copied_from": str(src)}
            else:
                stub = _dry_run_stub(row, args.arm, execution_mode, frontier_policy)
                with out.open("w", encoding="utf-8") as handle:
                    handle.write(json.dumps(stub) + "\n")
                payload = {"ok": True, "track": "live", "dry_run": True}
            summary, trace_error = _read_single_run_summary(out)
            payload["ok"] = trace_error is None
            if trace_error:
                payload["trace_error"] = trace_error
            payload.update(
                {
                    "task": task_id,
                    "arm": args.arm,
                    "trace_jsonl": _display_path(out),
                    "proof_accepted": summary.get("accepted") if summary else None,
                }
            )
            provenance.update(
                {
                    "status": "completed" if payload["ok"] else "failed",
                    "completed_at": _utc_now(),
                    "trace_error": trace_error,
                    "cli_returncode": None,
                }
            )
            _write_json_atomic(meta, provenance)
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
            model_timeout=args.model_timeout,
            proof_model=args.proof_model,
            proof_temperature=args.proof_temperature,
                proof_max_tokens=args.proof_max_tokens,
                enable_model_routing=needs_routing,
                strong_proof_model=args.strong_proof_model,
                action_cost_source=action_cost_source,
                remaining_budget_policy=remaining_budget_policy,
                cost_history_snapshot=history_path,
                out=out,
        )
        summary, trace_error = _read_single_run_summary(out)
        # CLI return code 1 means a completed but unaccepted proof run. It is a
        # benchmark outcome, not an infrastructure failure. Codes >=2 or an
        # invalid/missing trace are harness failures.
        run_ok = rc in {0, 1} and trace_error is None
        provenance.update(
            {
                "status": "completed" if run_ok else "failed",
                "completed_at": _utc_now(),
                "trace_error": trace_error,
                "cli_returncode": rc,
            }
        )
        _write_json_atomic(meta, provenance)
        results.append(
            {
                "ok": run_ok,
                "track": "live",
                "task": task_id,
                "arm": args.arm,
                "trace_jsonl": _display_path(out),
                "cli_returncode": rc,
                "proof_accepted": summary.get("accepted") if summary else None,
                "trace_error": trace_error,
            }
        )

    print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 0 if all(result.get("ok", False) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
