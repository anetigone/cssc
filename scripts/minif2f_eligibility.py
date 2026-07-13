"""Run the miniF2F per-task Lean elaboration eligibility gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.benchmarks.minif2f import MiniF2FError
from agent.benchmarks.minif2f_eligibility import run_minif2f_eligibility


def _rooted(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Elaborate every prepared miniF2F task independently with sorry."
    )
    parser.add_argument("--prepared-root", default="benchmark/generated/miniF2F")
    parser.add_argument("--project-root", default="benchmark/miniF2F")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--lake-executable")
    parser.add_argument(
        "--reuse-results",
        help="Prior eligibility results.jsonl; reuse only eligible rows with identical candidate hashes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def progress(index: int, total: int, task_id: str, status: str) -> None:
        if index == 1 or index % 25 == 0 or index == total or status != "eligible":
            print(f"[{index}/{total}] {task_id}: {status}", flush=True)

    try:
        summary = run_minif2f_eligibility(
            _rooted(args.prepared_root),
            _rooted(args.project_root),
            timeout_seconds=args.timeout,
            lake_executable=args.lake_executable,
            reuse_results=args.reuse_results,
            progress=progress,
        )
    except (MiniF2FError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    payload = {
        "ok": summary.ineligible == 0 and summary.infrastructure_failure == 0,
        "run_id": summary.run_id,
        "total": summary.total,
        "eligible": summary.eligible,
        "ineligible": summary.ineligible,
        "infrastructure_failure": summary.infrastructure_failure,
        "categories": summary.categories,
        "results": str(summary.results_path),
        "summary": str(summary.summary_path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
