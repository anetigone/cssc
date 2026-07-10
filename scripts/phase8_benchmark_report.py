"""Aggregate Phase 8.5 benchmark traces into a markdown summary.

Stage 0 skeleton (``tmp/phase8_5_benchmark_plan.md`` §9): it scans
``.runs/phase8/<suite>/<arm>/<task>/<rep>.jsonl``, reads the first
``run_summary`` event of each, and prints a per-(arm, task) markdown table with
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


def _read_run_summary(trace_path: Path) -> dict[str, Any] | None:
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "run_summary":
            return event
    return None


def _row_from_event(event: dict[str, Any], arm: str, task: str, rep: int) -> dict[str, Any]:
    metrics = event.get("metrics", {})
    metadata = event.get("metadata", {})
    result_summary = metadata.get("result_summary", {})
    tokens = metrics.get("model_input_tokens", 0) + metrics.get("model_output_tokens", 0)
    return {
        "arm": arm,
        "task": task,
        "rep": rep,
        "execution_mode": metrics.get("execution_mode", "?"),
        "frontier_policy": metadata.get("frontier_policy", "-"),
        "accepted": event.get("accepted"),
        "stop_reason": event.get("stop_reason", "?"),
        "model_calls": metrics.get("budget_model_calls_used", 0),
        "checks": metrics.get("budget_checks_used", 0),
        "tokens": tokens,
        "workspace_status": result_summary.get("workspace_status", "-"),
        "accepted_obligations": len(result_summary.get("accepted_obligations", [])),
        "open_obligations": len(result_summary.get("open_obligations", [])),
        "blocked_obligations": len(result_summary.get("blocked_obligations", [])),
    }


HEADERS = [
    "arm",
    "task",
    "rep",
    "mode",
    "policy",
    "accepted",
    "stop_reason",
    "model_calls",
    "checks",
    "tokens",
    "ws_status",
    "acc_obl",
    "open_obl",
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
            str(row["rep"]),
            row["execution_mode"],
            row["frontier_policy"],
            str(row["accepted"]),
            row["stop_reason"],
            str(row["model_calls"]),
            str(row["checks"]),
            str(row["tokens"]),
            row["workspace_status"],
            str(row["accepted_obligations"]),
            str(row["open_obligations"]),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "_Stage 0 skeleton: raw per-run rows only. Savings, CIs and verdicts "
        "arrive in Stage 4._"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=".runs/phase8")
    parser.add_argument("--suite-version", default=None, help="filter to one suite; omit for all")
    parser.add_argument("--output", default=None, help="write markdown here; default stdout")
    args = parser.parse_args()

    runs_dir = (ROOT / args.runs_dir).resolve()
    if not runs_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"runs dir not found: {runs_dir}"}))
        return 1

    rows: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    for path, suite, arm, task, rep in _iter_traces(runs_dir, args.suite_version):
        event = _read_run_summary(path)
        if event is None:
            parse_failures.append(str(path.relative_to(ROOT)))
            continue
        rows.append(_row_from_event(event, arm, task, rep))

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
                    "ok": True,
                    "rows": len(rows),
                    "output": str(out_path.relative_to(ROOT)),
                    "parse_failures": parse_failures,
                },
                indent=2,
            )
        )
    else:
        sys.stdout.write(markdown)
    if parse_failures:
        sys.stderr.write("parse failures (no run_summary):\n" + "\n".join(parse_failures) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
