"""Argument parser for the Lean task-solving CLI."""

from __future__ import annotations

import argparse

from .paths import ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Lean proof-completion tasks and solve one selected task."
    )
    parser.add_argument("source", nargs="?", default=None, help="Lean file or directory to scan.")
    parser.add_argument(
        "--agent-root",
        default=str(ROOT),
        help="Root for agent-owned config, runs, traces, and relative paths.",
    )
    parser.add_argument(
        "--task-config",
        default=None,
        help="JSON task config, usually under data/tasks/, with source/project_root/retrieval_source fields.",
    )
    parser.add_argument("--list-tasks", action="store_true", help="List extracted tasks and exit.")
    parser.add_argument("--task-index", type=int, default=0, help="Zero-based task index to solve.")
    parser.add_argument("--task-id", default=None, help="Task id to solve; overrides --task-index.")
    parser.add_argument("--split", default=None, help="Dataset split metadata for extracted tasks.")
    parser.add_argument("--hole-marker", default="{{proof}}")
    parser.add_argument("--allow-multiple-marker-tasks", action="store_true")
    parser.add_argument("--allow-multiple-sorry-tasks", action="store_true")
    parser.add_argument("--inactive-hole-fill", default="sorry")
    parser.add_argument("--pattern", default="*.lean", help="Directory scan pattern.")
    parser.add_argument(
        "--input-kind",
        choices=("auto", "lean", "natural_language"),
        default="auto",
        help="Interpret source/task config as Lean, natural-language prose, or infer from extension/config.",
    )
    parser.add_argument("--problem", default=None, help="Natural-language problem statement.")
    parser.add_argument(
        "--problem-file",
        default=None,
        help="UTF-8 file containing a natural-language problem statement.",
    )

    parser.add_argument("--candidate", action="append", default=[], help="Static proof candidate.")
    parser.add_argument(
        "--candidate-file",
        action="append",
        default=[],
        help="UTF-8 file containing one static proof candidate.",
    )
    parser.add_argument("--use-model", action="store_true", help="Use OpenAI-compatible chat config.")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--max-candidates", type=int, default=1)
    parser.add_argument("--max-checks", type=int, default=1)
    parser.add_argument("--max-model-calls", type=int, default=1)
    parser.add_argument("--max-repair-rounds", type=int, default=2)
    parser.add_argument("--enable-retrieval", action="store_true", help="Use lexical retrieval over local Lean files.")
    parser.add_argument(
        "--retrieval-source",
        action="append",
        default=[],
        help="Lean file or directory to index for retrieval. Defaults to --source when retrieval is enabled.",
    )
    parser.add_argument("--max-retrieval-results", type=int, default=5)
    parser.add_argument(
        "--retrieve-before-first-model-call",
        action="store_true",
        help="Retrieve local snippets before the first model proposal.",
    )
    parser.add_argument("--lean-timeout", type=float, default=10.0)
    parser.add_argument("--max-elapsed-seconds", type=float, default=None)
    parser.add_argument("--project-root", default=None, help="Lake project root; auto-detected by default.")
    parser.add_argument("--no-lake", action="store_true", help="Call lean directly instead of lake env lean.")
    parser.add_argument(
        "--no-lean-server",
        action="store_true",
        help="Disable the persistent Lean language server and run a fresh lean process per check.",
    )
    parser.add_argument("--allow-sorry", action="store_true", help="Do not reject remaining sorry warnings.")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Agent-side directory for archived candidates. Defaults to AGENT_ROOT/.runs.",
    )
    parser.add_argument(
        "--check-work-dir",
        default=None,
        help=(
            "Checker-side temporary directory, resolved under --project-root when relative. "
            "Defaults to PROJECT_ROOT/.checks when a Lake project is used."
        ),
    )
    parser.add_argument(
        "--keep-check-files",
        action="store_true",
        help="Keep checker-side temporary Lean files for debugging.",
    )
    parser.add_argument("--trace-jsonl", default=None, help="Append controller trace events to JSONL.")
    parser.add_argument(
        "--trace-raw-output",
        action="store_true",
        help="Include raw checker output in JSONL traces.",
    )
    parser.add_argument("--log-level", default="WARNING", help="Python logging level.")
    parser.add_argument("--log-file", default=None, help="Optional file for debug logs.")
    return parser
