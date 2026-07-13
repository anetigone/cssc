"""Prepare or validate an ignored external miniF2F checkout without running Lean."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.benchmarks.minif2f import (
    MiniF2FError,
    prepare_minif2f,
    resolve_minif2f_root,
    validate_prepared_minif2f,
)

def _path_from_root(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare miniF2F as 488 independent single-hole Lean tasks."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="extract fixtures and write manifest/provenance")
    prepare.add_argument("--source-root", default="benchmark/miniF2F")
    prepare.add_argument("--output-root", default="benchmark/generated/miniF2F")
    prepare.add_argument("--allow-dirty-source", action="store_true")
    validate = subparsers.add_parser("validate", help="validate prepared files without invoking Lean")
    validate.add_argument("--output-root", default="benchmark/generated/miniF2F")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            source_root = resolve_minif2f_root(args.source_root, repository_root=ROOT)
            suite = prepare_minif2f(
                source_root,
                _path_from_root(args.output_root),
                allow_dirty_source=args.allow_dirty_source,
            )
            result = {
                "ok": True,
                "command": "prepare",
                "source_revision": suite.source_revision,
                "split_counts": suite.split_counts,
                "manifest": str(suite.manifest_path),
                "provenance": str(suite.provenance_path),
                "lean_invoked": False,
                "eligibility": "not_checked",
            }
        else:
            output_root = _path_from_root(args.output_root)
            counts = validate_prepared_minif2f(output_root)
            provenance = json.loads(
                (output_root / "provenance.json").read_text(encoding="utf-8")
            )
            result = {
                "ok": True,
                "command": "validate",
                "split_counts": counts,
                "lean_invoked": False,
                "eligibility": provenance.get("eligibility", "not_checked"),
            }
    except (MiniF2FError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
