"""Argument parser for the staged Lean proof-agent CLI."""

from __future__ import annotations

import argparse

from .paths import ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cssc",
        description="Formalize natural-language mathematics and search for Lean proofs.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    solve = subcommands.add_parser(
        "solve",
        help="Run the complete formalize -> prove pipeline.",
        description="Solve a natural-language problem or Lean proof-completion task end to end.",
    )
    _add_input_args(solve)
    _add_task_selection_args(solve, include_all=False)
    _add_runtime_args(solve)
    _add_formalization_args(solve, include_model_toggle=False)
    _add_proof_args(solve, include_model_toggle=True)
    _add_model_args(solve, role=None)
    _add_output_arg(solve)

    formalize = subcommands.add_parser(
        "formalize",
        help="Convert natural-language input into a Lean scaffold.",
        description="Formalize one selected natural-language task, or all tasks with --all.",
    )
    _add_input_args(formalize)
    _add_task_selection_args(formalize, include_all=True)
    _add_runtime_args(formalize)
    _add_formalization_args(formalize, include_model_toggle=True)
    _add_model_args(formalize, role=None)
    _add_output_arg(formalize)

    prove = subcommands.add_parser(
        "prove",
        help="Run proof search on a Lean scaffold or formalization artifact.",
        description="Prove one selected Lean task from a file, config, or formalization artifact.",
    )
    _add_input_args(prove, natural_language=False)
    _add_task_selection_args(prove, include_all=False)
    _add_runtime_args(prove)
    _add_proof_args(prove, include_model_toggle=True)
    _add_model_args(prove, role=None)
    _add_output_arg(prove)
    return parser


def _add_input_args(parser: argparse.ArgumentParser, *, natural_language: bool = True) -> None:
    group = parser.add_argument_group("input")
    group.add_argument("source", nargs="?", default=None, help="Input file or directory.")
    group.add_argument("--task-config", default=None, help="JSON task config or stage artifact.")
    choices = ("auto", "lean", "natural_language") if natural_language else ("auto", "lean")
    group.add_argument("--input-kind", choices=choices, default="auto")
    if natural_language:
        group.add_argument("--problem", default=None, help="Inline natural-language problem.")
        group.add_argument("--problem-file", default=None, help="UTF-8 natural-language problem file.")
    group.add_argument("--pattern", default="*.lean", help="Directory scan pattern.")
    group.add_argument("--split", default=None)
    group.add_argument("--hole-marker", default="{{proof}}")
    group.add_argument("--allow-multiple-marker-tasks", action="store_true")
    group.add_argument("--allow-multiple-sorry-tasks", action="store_true")
    group.add_argument("--inactive-hole-fill", default="sorry")


def _add_task_selection_args(parser: argparse.ArgumentParser, *, include_all: bool) -> None:
    group = parser.add_argument_group("task selection")
    group.add_argument("--list-tasks", action="store_true")
    group.add_argument("--task-index", type=int, default=0)
    group.add_argument("--task-id", default=None)
    if include_all:
        group.add_argument("--all", action="store_true", dest="all_tasks", help="Process every input task.")


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("runtime")
    group.add_argument("--agent-root", default=str(ROOT))
    group.add_argument("--env-file", default=str(ROOT / ".env"))
    group.add_argument("--project-root", default=None)
    group.add_argument("--lean-timeout", type=float, default=10.0)
    group.add_argument("--model-timeout", type=float, default=60.0)
    group.add_argument("--scaffold-timeout", type=float, default=None)
    group.add_argument("--no-lake", action="store_true")
    group.add_argument("--lake-executable", default=None)
    group.add_argument("--lean-executable", default=None)
    group.add_argument("--no-lean-server", action="store_true")
    group.add_argument("--lean-server-startup-timeout", type=float, default=60.0)
    group.add_argument(
        "--lean-server-fallback-seconds",
        type=float,
        default=2.0,
        help="How long to wait after the latest diagnostics before accepting them "
        "without an explicit fileProgress completion signal.",
    )
    group.add_argument("--allow-sorry", action="store_true")
    group.add_argument("--check-work-dir", default=None)
    group.add_argument("--keep-check-files", action="store_true")
    group.add_argument("--log-level", default="WARNING")
    group.add_argument("--log-file", default=None)
    group.add_argument("--run-name", default=None)


def _add_model_args(
    parser: argparse.ArgumentParser,
    *,
    role: str | None,
    default_max_tokens: int | None = None,
) -> None:
    prefix = f"{role}-" if role else ""
    dest_prefix = f"{role}_" if role else ""
    group = parser.add_argument_group(f"{role or 'stage'} model")
    group.add_argument(f"--{prefix}model", dest=f"{dest_prefix}model", default=None)
    group.add_argument(
        f"--{prefix}temperature", dest=f"{dest_prefix}temperature", type=float, default=None
    )
    group.add_argument(
        f"--{prefix}max-tokens",
        dest=f"{dest_prefix}max_tokens",
        type=int,
        default=default_max_tokens,
    )


def _add_formalization_args(
    parser: argparse.ArgumentParser, *, include_model_toggle: bool
) -> None:
    group = parser.add_argument_group("formalization")
    if include_model_toggle:
        _add_model_toggle(group)
    group.add_argument("--no-check", action="store_true", help="Skip Lean scaffold validation.")
    group.add_argument("--formalization-cache-dir", default=None)
    group.add_argument("--formalization-cache", action="store_true")
    group.add_argument("--no-formalization-cache", action="store_true")
    _add_model_args(parser, role="formalizer")


def _add_proof_args(parser: argparse.ArgumentParser, *, include_model_toggle: bool) -> None:
    group = parser.add_argument_group("proof search")
    if include_model_toggle:
        _add_model_toggle(group)
    group.add_argument("--candidate", action="append", default=[])
    group.add_argument("--candidate-file", action="append", default=[])
    group.add_argument("--max-candidates", type=int, default=1)
    group.add_argument("--max-model-calls", type=int, default=3)
    group.add_argument("--max-checks", type=int, default=3)
    group.add_argument("--max-elapsed-seconds", type=float, default=None)
    group.add_argument("--max-input-tokens", type=float, default=None)
    group.add_argument("--max-output-tokens", type=float, default=None)
    group.add_argument("--max-billed-tokens", type=float, default=None)
    group.add_argument("--max-api-cost-usd", type=float, default=None)
    group.add_argument("--global-reserve-checks", type=int, default=0)
    group.add_argument("--global-reserve-model-requests", type=int, default=0)
    group.add_argument(
        "--execution-mode",
        choices=("minimal", "structured"),
        default="minimal",
        help="执行模式，默认 minimal；structured 为结构化搜索模式。",
    )
    group.add_argument(
        "--frontier-policy",
        choices=(
            "legacy",
            "cost_aware_v1",
            "cost_aware_v2",
            "value_per_cost_v1",
            "action_cost_aware_v1",
        ),
        default="legacy",
        help="Structured frontier 排序策略，默认 legacy；"
        "minimal 模式忽略此参数。cost_aware_v1/v2/value_per_cost_v1/action_cost_aware_v1 为 opt-in 档位，"
        "仅影响调度顺序，不改变证明语义。",
    )
    group.add_argument("--enable-retrieval", action="store_true")
    group.add_argument("--retrieval-source", action="append", default=[])
    group.add_argument("--max-retrieval-results", type=int, default=5)
    group.add_argument("--retrieve-before-first-model-call", action="store_true")
    group.add_argument("--context-summarizer", action="store_true", dest="context_summarizer")
    group.add_argument(
        "--no-context-summarizer",
        action="store_false",
        dest="context_summarizer",
        help="Disable the lightweight context-summarizer agent.",
    )
    group.set_defaults(context_summarizer=False)
    group.add_argument("--work-dir", default=None)
    group.add_argument("--trace-jsonl", default=None)
    group.add_argument("--trace-raw-output", action="store_true")
    group.add_argument(
        "--enable-model-routing",
        action="store_true",
        help="Enable opt-in cheap/strong routing for action_cost_aware_v1.",
    )
    group.add_argument("--strong-proof-model", default=None)
    group.add_argument("--strong-proof-temperature", type=float, default=None)
    group.add_argument("--strong-proof-max-tokens", type=int, default=None)
    group.add_argument(
        "--action-cost-source",
        choices=("auto", "static", "empirical"),
        default="auto",
        help="Cost source for action_cost_aware_v1.",
    )
    group.add_argument(
        "--cost-history-snapshot",
        default=None,
        help="Frozen CostHistorySnapshot JSON; required for empirical cost.",
    )
    budget_policy = group.add_mutually_exclusive_group()
    budget_policy.add_argument(
        "--enable-remaining-budget-policy",
        action="store_true",
        dest="remaining_budget_policy",
    )
    budget_policy.add_argument(
        "--disable-remaining-budget-policy",
        action="store_false",
        dest="remaining_budget_policy",
    )
    group.set_defaults(remaining_budget_policy=True)
    _add_model_args(parser, role="proof")
    _add_model_args(parser, role="context", default_max_tokens=512)


def _add_output_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output", default=None, help="Also write the JSON result to this file.")


def _add_model_toggle(group: argparse._ArgumentGroup) -> None:
    toggle = group.add_mutually_exclusive_group()
    toggle.add_argument("--use-model", action="store_true", dest="use_model", help=argparse.SUPPRESS)
    toggle.add_argument(
        "--no-model",
        "--no-use-model",
        action="store_false",
        dest="use_model",
        help="Disable model calls; proof commands then require static candidates.",
    )
    group.set_defaults(use_model=None)
