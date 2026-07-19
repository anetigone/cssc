"""Run a miniF2F split in one process with one persistent Lean server."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.benchmarks.minif2f import MiniF2FError
from agent.benchmarks.minif2f_runner import (
    refresh_minif2f_run_index,
    run_minif2f_benchmark,
)


def _rooted(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run eligible miniF2F tasks without per-task Lean cold starts. "
        "Put ordinary `cssc prove` options after `--`."
    )
    parser.add_argument("--prepared-root", default="benchmark/generated/miniF2F")
    parser.add_argument("--project-root", default="benchmark/miniF2F")
    parser.add_argument("--split", choices=("valid", "test"))
    parser.add_argument(
        "--refresh-index",
        metavar="RUN_ROOT",
        help="Regenerate README.md and task-index.csv for an existing run, then exit.",
    )
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--run-name")
    parser.add_argument(
        "--execution-mode",
        choices=("minimal", "structured"),
        default=None,
        help="Proof-search mode for this benchmark run (default: minimal).",
    )
    parser.add_argument("--resume", help="Existing .runs benchmark directory to resume.")
    parser.add_argument(
        "--retry-infrastructure-failures",
        action="store_true",
        default=None,
        help="With --resume, rerun saved infrastructure failures (the default).",
    )
    parser.add_argument(
        "--skip-infrastructure-failures",
        action="store_false",
        dest="retry_infrastructure_failures",
        help="With --resume, keep saved infrastructure failures instead of rerunning them.",
    )
    parser.add_argument(
        "--retry-transient-generation-failures",
        action="store_true",
        default=None,
        help="With --resume, rerun saved truncated model outputs (the default).",
    )
    parser.add_argument(
        "--skip-transient-generation-failures",
        action="store_false",
        dest="retry_transient_generation_failures",
        help="With --resume, keep saved truncated-output failures.",
    )
    parser.add_argument("--continue-on-infrastructure-failure", action="store_true")
    parser.add_argument("proof_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh_index:
        run_root = _rooted(args.refresh_index)
        try:
            refresh_minif2f_run_index(run_root)
        except (MiniF2FError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(
            json.dumps(
                {
                    "ok": True,
                    "run_root": str(run_root),
                    "readme": str(run_root / "README.md"),
                    "task_index": str(run_root / "task-index.csv"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.split is None:
        raise SystemExit("--split is required unless --refresh-index is used.")
    proof_args = list(args.proof_args)
    if proof_args[:1] == ["--"]:
        proof_args.pop(0)
    proof_args_has_execution_mode = any(
        arg == "--execution-mode" or arg.startswith("--execution-mode=")
        for arg in proof_args
    )
    if proof_args_has_execution_mode and args.execution_mode is not None:
        raise SystemExit(
            "Specify --execution-mode either before or after `--`, not both."
        )
    if args.execution_mode is not None:
        proof_args[0:0] = ["--execution-mode", args.execution_mode]
    if args.resume:
        run_root = _rooted(args.resume)
        resume = True
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = args.run_name or f"{stamp}-{args.split}"
        run_root = ROOT / ".runs" / "benchmarks" / "minif2f" / name
        resume = False

    def progress(index: int, total: int, task_id: str, status: str) -> None:
        print(f"[{index}/{total}] {task_id}: {status}", flush=True)

    try:
        summary = run_minif2f_benchmark(
            _rooted(args.prepared_root),
            _rooted(args.project_root),
            run_root,
            split=args.split,
            proof_args=proof_args,
            task_ids=args.task_id,
            offset=args.offset,
            limit=args.limit,
            resume=resume,
            retry_infrastructure_failures=args.retry_infrastructure_failures,
            retry_transient_generation_failures=(
                args.retry_transient_generation_failures
            ),
            continue_on_infrastructure_failure=args.continue_on_infrastructure_failure,
            progress=progress,
        )
    except (MiniF2FError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "ok": False,
                    "interrupted": True,
                    "run_root": str(run_root),
                    "status": "interrupted",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 130
    payload = {
        "ok": summary.infrastructure_failures == 0 and summary.completed == summary.selected,
        "run_id": summary.run_id,
        "run_root": str(summary.run_root),
        "selected": summary.selected,
        "completed": summary.completed,
        "accepted": summary.accepted,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "infrastructure_failures": summary.infrastructure_failures,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
