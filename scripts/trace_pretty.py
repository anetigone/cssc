"""Print controller JSONL traces in a failure-first human-readable form."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.runtime.trace_pretty import TraceReadError, format_trace, read_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render trace.jsonl with errors and timing before verbose metadata."
    )
    parser.add_argument("trace", nargs="+", help="One or more trace.jsonl files.")
    parser.add_argument("--latest", action="store_true", help="Show only the last appended run.")
    parser.add_argument("--show-proof", action="store_true", help="Include candidate proof text.")
    parser.add_argument("--show-cost", action="store_true", help="Include concise ledger totals.")
    parser.add_argument("--raw-events", action="store_true", help="Append fully indented JSON events.")
    parser.add_argument("--max-message-chars", type=int, default=2_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_message_chars < 100:
        print("error: --max-message-chars must be at least 100", file=sys.stderr)
        return 2
    outputs: list[str] = []
    try:
        for value in args.trace:
            path = Path(value)
            if path.is_dir():
                path = path / "trace.jsonl"
            outputs.append(
                format_trace(
                    read_trace(path),
                    source=path,
                    latest=args.latest,
                    show_proof=args.show_proof,
                    show_cost=args.show_cost,
                    raw_events=args.raw_events,
                    max_message_chars=args.max_message_chars,
                )
            )
    except TraceReadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n\n".join(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
