"""Aggregate Phase 8.5 benchmark traces into a markdown summary.

Stage 0 skeleton (``tmp/phase8_5_benchmark_plan.md`` §9): it scans
``.runs/phase8/<suite>/<arm>/<task>/<rep>.jsonl``, requires exactly one
``run_summary`` event in each, and prints a per-run markdown table with
the cost metrics and workspace status pulled from the trace.

Stage 0 deliberately does **not** compute savings, confidence intervals, or
verdicts — those arrive in Stage 4 (frozen conclusions). This script only proves
the aggregation + formatting path works against real trace fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _iter_traces(runs_dir: Path, suite_filter: str | None):
    for path in sorted(runs_dir.rglob("*.jsonl")):
        try:
            rel = path.relative_to(runs_dir)
            suite, arm, task = rel.parts[0], rel.parts[1], rel.parts[2]
            rep = int(rel.stem)
        except (ValueError, IndexError):
            continue
        if suite_filter and suite != suite_filter:
            continue
        yield path, suite, arm, task, rep


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest line {line_number}: invalid JSON: {exc.msg}") from exc
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"manifest line {line_number}: missing task_id")
        if task_id in rows:
            raise ValueError(f"manifest line {line_number}: duplicate task_id {task_id!r}")
        rows[task_id] = row
    return rows


def _read_trace(trace_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        trace_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at line {line_number}: {exc.msg}") from exc
        if event.get("event") == "run_summary":
            summaries.append(event)
        elif event.get("event") == "attempt" and isinstance(event.get("attempt"), dict):
            attempts.append(event["attempt"])
    if len(summaries) != 1:
        raise ValueError(f"expected exactly one run_summary, found {len(summaries)}")
    return summaries[0], attempts


def _provenance_path(trace_path: Path) -> Path:
    return trace_path.with_suffix(".meta.json")


def _read_provenance(
    trace_path: Path,
    *,
    suite: str,
    arm: str,
    task: str,
    rep: int,
) -> dict[str, Any]:
    path = _provenance_path(trace_path)
    if not path.is_file():
        raise ValueError(f"missing provenance sidecar: {_display_path(path)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid provenance JSON: {exc.msg}") from exc
    expected = {
        "suite_version": suite,
        "arm": arm,
        "task_id": task,
        "repetition": rep,
    }
    mismatches = [
        f"{key}={payload.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if payload.get(key) != value
    ]
    if payload.get("status") != "completed":
        mismatches.append(f"status={payload.get('status')!r} (expected 'completed')")
    if mismatches:
        raise ValueError("provenance mismatch: " + ", ".join(mismatches))
    return payload


def _goal_attained(
    event: dict[str, Any],
    manifest_row: dict[str, Any],
    result_summary: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> bool:
    expected = manifest_row.get("expected_terminal")
    accepted_obligations = result_summary.get("accepted_obligations", [])
    min_helpers = manifest_row.get("expected_min_accepted_helpers", 0)
    if expected == "accepted":
        return event.get("accepted") is True
    if expected == "blocked":
        if result_summary.get("workspace_status") != "blocked":
            return False
        expected_category = manifest_row.get("expected_block_category")
        expected_signature = manifest_row.get("expected_probe_signature")
        capability_attempts = [
            attempt
            for attempt in attempts
            if (attempt.get("edit") or {}).get("action") == "capability_test"
        ]
        if not capability_attempts:
            return False
        return any(
            (
                expected_category is None
                or (attempt.get("check_result") or {}).get("category")
                == expected_category
            )
            and (
                expected_signature is None
                or expected_signature in str((attempt.get("edit") or {}).get("text", ""))
            )
            for attempt in capability_attempts
        )
    if expected == "partial":
        return (
            result_summary.get("workspace_status") == "partial"
            and len(accepted_obligations) >= min_helpers
        )
    return False


def _row_from_event(
    event: dict[str, Any],
    suite: str,
    arm: str,
    task: str,
    rep: int,
    manifest_row: dict[str, Any],
    provenance: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = event.get("metrics") or {}
    metadata = event.get("metadata") or {}
    result_summary = metadata.get("result_summary") or {}
    tokens = metrics.get("model_input_tokens", 0) + metrics.get("model_output_tokens", 0)
    return {
        "suite": suite,
        "arm": arm,
        "task": task,
        "layer": manifest_row.get("layer", "?"),
        "rep": rep,
        "execution_mode": metrics.get("execution_mode", "?"),
        "frontier_policy": metadata.get("frontier_policy", "-"),
        "accepted": event.get("accepted"),
        "expected_terminal": manifest_row.get("expected_terminal", "?"),
        "goal_attained": _goal_attained(
            event, manifest_row, result_summary, attempts
        ),
        "stop_reason": event.get("stop_reason", "?"),
        "model_calls": metrics.get("budget_model_calls_used", 0),
        "checks": metrics.get("budget_checks_used", 0),
        "tokens": tokens,
        "workspace_status": result_summary.get("workspace_status", "-"),
        "accepted_obligations": len(result_summary.get("accepted_obligations", [])),
        "open_obligations": len(result_summary.get("open_obligations", [])),
        "blocked_obligations": len(result_summary.get("blocked_obligations", [])),
        "git_commit": provenance.get("git_commit", "-") if provenance else "-",
        "proof_model": provenance.get("proof_model", "-") if provenance else "-",
    }


HEADERS = [
    "arm",
    "task",
    "layer",
    "rep",
    "mode",
    "policy",
    "accepted",
    "expected",
    "attained",
    "stop_reason",
    "model_calls",
    "checks",
    "tokens",
    "ws_status",
    "acc_obl",
    "open_obl",
    "blocked_obl",
]


def _render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "# Phase 8.5 benchmark report\n\n(no traces found)\n"

    lines = ["# Phase 8.5 benchmark report", ""]
    lines.append("| " + " | ".join(HEADERS) + " |")
    lines.append("|" + "|".join(["---"] * len(HEADERS)) + "|")
    for row in rows:
        cells = [
            row["arm"],
            row["task"],
            row["layer"],
            str(row["rep"]),
            row["execution_mode"],
            row["frontier_policy"],
            str(row["accepted"]),
            row["expected_terminal"],
            str(row["goal_attained"]),
            row["stop_reason"],
            str(row["model_calls"]),
            str(row["checks"]),
            str(row["tokens"]),
            row["workspace_status"],
            str(row["accepted_obligations"]),
            str(row["open_obligations"]),
            str(row["blocked_obligations"]),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "_Stage 0 skeleton: raw per-run rows only. Savings, CIs and verdicts "
        "arrive in Stage 4._"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=".runs/phase8")
    parser.add_argument(
        "--manifest",
        default="tests/fixtures/phase8_benchmark/manifest.jsonl",
    )
    parser.add_argument("--suite-version", default=None, help="filter to one suite; omit for all")
    parser.add_argument("--output", default=None, help="write markdown here; default stdout")
    parser.add_argument(
        "--allow-missing-provenance",
        action="store_true",
        help="Allow legacy traces without a .meta.json sidecar.",
    )
    args = parser.parse_args(argv)

    runs_dir = (ROOT / args.runs_dir).resolve()
    if not runs_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"runs dir not found: {runs_dir}"}))
        return 1
    manifest_path = (ROOT / args.manifest).resolve()
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    rows: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    for path, suite, arm, task, rep in _iter_traces(runs_dir, args.suite_version):
        manifest_row = manifest.get(task)
        if manifest_row is None:
            parse_failures.append(f"{_display_path(path)}: task not found in manifest")
            continue
        try:
            event, attempts = _read_trace(path)
            provenance = None
            if not args.allow_missing_provenance or _provenance_path(path).is_file():
                provenance = _read_provenance(
                    path, suite=suite, arm=arm, task=task, rep=rep
                )
        except (OSError, ValueError) as exc:
            parse_failures.append(f"{_display_path(path)}: {exc}")
            continue
        rows.append(
            _row_from_event(
                event, suite, arm, task, rep, manifest_row, provenance, attempts
            )
        )

    # stable order: arm, task, rep
    rows.sort(key=lambda r: (r["arm"], r["task"], r["rep"]))
    markdown = _render(rows)

    if args.output:
        out_path = (ROOT / args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": not parse_failures and bool(rows),
                    "rows": len(rows),
                    "output": _display_path(out_path),
                    "parse_failures": parse_failures,
                },
                indent=2,
            )
        )
    else:
        sys.stdout.write(markdown)
    if parse_failures:
        sys.stderr.write("trace validation failures:\n" + "\n".join(parse_failures) + "\n")
    if not rows:
        sys.stderr.write("no valid traces found\n")
    return 0 if rows and not parse_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
