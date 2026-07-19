"""Summarize outcomes and token usage for an existing miniF2F run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.benchmarks.minif2f import MiniF2FError
from agent.benchmarks.minif2f_run_report import atomic_json, atomic_text
from agent.benchmarks.minif2f_usage_report import (
    build_minif2f_usage_report,
    render_minif2f_usage_markdown,
)


def _rooted(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate current miniF2F results and append-only resume history. "
            "Missing token usage remains explicitly incomplete."
        )
    )
    parser.add_argument("run_root", help="Existing miniF2F benchmark run directory.")
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Stdout format (default: json).",
    )
    parser.add_argument("--json-output", help="Optionally write the full JSON report.")
    parser.add_argument(
        "--markdown-output",
        help="Optionally write a human-readable Markdown report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_root = _rooted(args.run_root)
    try:
        report = build_minif2f_usage_report(run_root)
        markdown = render_minif2f_usage_markdown(report)
        if args.json_output:
            atomic_json(_rooted(args.json_output), report)
        if args.markdown_output:
            atomic_text(_rooted(args.markdown_output), markdown)
    except (MiniF2FError, OSError, ValueError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)
        )
        return 2

    if args.format == "markdown":
        print(markdown, end="")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
