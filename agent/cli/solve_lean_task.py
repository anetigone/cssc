"""Build Lean proof tasks from files and solve one selected task.

Examples:
    python solve_lean_task.py lean_workspace/Cssc/Tasks/Basic.lean --list-tasks
    python solve_lean_task.py Basic.lean --task-index 0 --candidate trivial
    python solve_lean_task.py Basic.lean --use-model
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent import (
    BudgetConfig,
    ControllerConfig,
    LeanAdapter,
    LeanTaskBuilder,
    ModelAdapterError,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
    ProofController,
    ProofTask,
    StaticActionGenerator,
    TaskBuildError,
    TaskBuilderConfig,
)
from agent.runtime.env_loader import load_dotenv
from agent.runtime.logging_config import configure_logging
from agent.runtime.trace_store import JsonlTraceStore
from agent.runtime.workspace import AttemptWorkspace


ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_logging(level=args.log_level, log_file=args.log_file)
    except ValueError as exc:
        print(json.dumps({"ok": False, "stage": "logging_config", "error": str(exc)}, indent=2))
        return 2

    logger.info("CLI started: source=%s use_model=%s", args.source, args.use_model)

    try:
        tasks = build_tasks(args)
        logger.info("Built %d task(s) from %s", len(tasks), args.source)
        if args.list_tasks:
            payload = {"tasks": [_task_summary(task, index) for index, task in enumerate(tasks)]}
            print(json.dumps(payload, indent=2))
            return 0

        task = select_task(tasks, task_id=args.task_id, task_index=args.task_index)
        logger.info("Selected task: task_id=%s", task.task_id)
        generator = build_action_generator(args)
    except (TaskBuildError, ValueError, ModelAdapterError) as exc:
        logger.exception("CLI setup failed")
        print(json.dumps({"ok": False, "stage": "setup", "error": str(exc)}, indent=2))
        return 2

    project_root = Path(args.project_root).resolve() if args.project_root else find_lake_root(args.source)
    logger.debug("Using project_root=%s", project_root)
    with _workspace_context(args.work_dir) as work_dir:
        logger.debug("Using attempt workspace: %s", work_dir)
        controller = ProofController(
            adapter=LeanAdapter(
                project_root=project_root,
                prefer_lake=not args.no_lake,
                disallow_sorry=not args.allow_sorry,
            ),
            action_generator=generator,
            workspace=AttemptWorkspace(work_dir),
            budget_config=BudgetConfig(
                max_checks=args.max_checks,
                max_model_calls=args.max_model_calls,
                per_check_timeout_seconds=args.lean_timeout,
                max_elapsed_seconds=args.max_elapsed_seconds,
            ),
            config=ControllerConfig(max_candidates_per_model_call=args.max_candidates),
        )
        try:
            result = controller.run(task)
        except ModelAdapterError as exc:
            logger.exception("Controller run failed during model call")
            print(json.dumps({"ok": False, "stage": "run", "error": str(exc)}, indent=2))
            return 2

    if args.trace_jsonl:
        logger.info("Appending controller trace: %s", args.trace_jsonl)
        JsonlTraceStore(args.trace_jsonl, include_raw_output=args.trace_raw_output).append_result(result)

    logger.info(
        "CLI finished: task_id=%s accepted=%s stop_reason=%s attempts=%d",
        result.task.task_id,
        result.accepted,
        result.stop_reason,
        len(result.attempts),
    )
    print(json.dumps(_result_payload(result, include_candidate_file=args.work_dir is not None), indent=2))
    return 0 if result.accepted else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Lean proof-completion tasks and solve one selected task."
    )
    parser.add_argument("source", help="Lean file or directory to scan.")
    parser.add_argument("--list-tasks", action="store_true", help="List extracted tasks and exit.")
    parser.add_argument("--task-index", type=int, default=0, help="Zero-based task index to solve.")
    parser.add_argument("--task-id", default=None, help="Task id to solve; overrides --task-index.")
    parser.add_argument("--split", default=None, help="Dataset split metadata for extracted tasks.")
    parser.add_argument("--hole-marker", default="{{proof}}")
    parser.add_argument("--allow-multiple-marker-tasks", action="store_true")
    parser.add_argument("--allow-multiple-sorry-tasks", action="store_true")
    parser.add_argument("--inactive-hole-fill", default="sorry")
    parser.add_argument("--pattern", default="*.lean", help="Directory scan pattern.")

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
    parser.add_argument("--lean-timeout", type=float, default=10.0)
    parser.add_argument("--max-elapsed-seconds", type=float, default=None)
    parser.add_argument("--project-root", default=None, help="Lake project root; auto-detected by default.")
    parser.add_argument("--no-lake", action="store_true", help="Call lean directly instead of lake env lean.")
    parser.add_argument("--allow-sorry", action="store_true", help="Do not reject remaining sorry warnings.")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Directory for materialized candidates. If omitted, a temporary directory is used.",
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


def build_tasks(args: argparse.Namespace) -> list[ProofTask]:
    source = Path(args.source)
    config = TaskBuilderConfig(
        hole_marker=args.hole_marker,
        inactive_hole_fill=args.inactive_hole_fill,
        allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
        allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
    )
    builder = LeanTaskBuilder(config)
    if source.is_dir():
        tasks = builder.build_from_directory(source, split=args.split, pattern=args.pattern)
    else:
        tasks = builder.build_from_file(source, split=args.split)
    if not tasks:
        raise TaskBuildError(f"No tasks were extracted from {source}.")
    return tasks


def select_task(
    tasks: list[ProofTask],
    *,
    task_id: str | None = None,
    task_index: int = 0,
) -> ProofTask:
    if task_id is not None:
        for task in tasks:
            if task.task_id == task_id:
                return task
        raise ValueError(f"Task id not found: {task_id}")
    if task_index < 0 or task_index >= len(tasks):
        raise ValueError(f"Task index {task_index} is out of range for {len(tasks)} tasks.")
    return tasks[task_index]


def build_action_generator(args: argparse.Namespace):
    candidates = list(args.candidate)
    for path in args.candidate_file:
        candidates.append(Path(path).read_text(encoding="utf-8"))

    if candidates and args.use_model:
        raise ValueError("Use either static candidates or --use-model, not both.")
    if candidates:
        logger.debug("Using %d static candidate(s)", len(candidates))
        return StaticActionGenerator(candidates)
    if args.use_model:
        env_path = Path(args.env_file)
        if env_path.exists():
            logger.debug("Loading environment file: %s", env_path)
            load_dotenv(env_path, override=False)
        else:
            logger.debug("Environment file does not exist: %s", env_path)
        return OpenAIChatActionGenerator(OpenAIChatConfig.from_env())
    raise ValueError("Provide --candidate, --candidate-file, or --use-model.")


def find_lake_root(source: str) -> Path | None:
    path = Path(source).resolve()
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / "lakefile.lean").exists() or (candidate / "lakefile.toml").exists():
            return candidate
    return None


@contextmanager
def _workspace_context(work_dir: str | None) -> Iterator[Path]:
    if work_dir is None:
        with tempfile.TemporaryDirectory() as tmp:
            yield Path(tmp)
        return
    path = Path(work_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    yield path


def _task_summary(task: ProofTask, index: int) -> dict[str, object]:
    return {
        "index": index,
        "task_id": task.task_id,
        "source_file": task.metadata.get("source_file"),
        "hole_kind": task.metadata.get("hole_kind"),
        "hole_line": task.metadata.get("hole_line"),
        "hole_column": task.metadata.get("hole_column"),
        "source_hole_count": task.metadata.get("source_hole_count"),
    }


def _result_payload(result, *, include_candidate_file: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": result.accepted,
        "task_id": result.task.task_id,
        "stop_reason": result.stop_reason,
        "attempts": len(result.attempts),
        "checks_used": result.budget.checks_used,
        "model_calls_used": result.budget.model_calls_used,
    }
    if result.accepted_attempt is not None and include_candidate_file:
        payload["accepted_candidate_file"] = str(result.accepted_attempt.candidate_file)
    if result.accepted_attempt is not None:
        payload["accepted_proof"] = result.accepted_attempt.edit.text
    if result.attempts:
        last = result.attempts[-1].check_result
        payload["last_category"] = last.category.value
        payload["last_message"] = last.parsed_feedback.message if last.parsed_feedback else ""
    return payload
