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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agent import (
    BudgetConfig,
    ControllerConfig,
    LeanAdapter,
    LeanTaskBuilder,
    LexicalLeanRetriever,
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
from agent.runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace


ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args = apply_task_config(args)
        agent_root = resolve_agent_root(args.agent_root)
        args.agent_root = str(agent_root)
        if args.log_file:
            args.log_file = str(resolve_agent_path(agent_root, args.log_file))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "stage": "task_config", "error": str(exc)}, indent=2))
        return 2

    try:
        configure_logging(level=args.log_level, log_file=args.log_file)
    except ValueError as exc:
        print(json.dumps({"ok": False, "stage": "logging_config", "error": str(exc)}, indent=2))
        return 2

    logger.info("CLI started: source=%s task_config=%s use_model=%s", args.source, args.task_config, args.use_model)

    try:
        tasks = build_tasks(args)
        logger.info("Built %d task(s) from task input", len(tasks))
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

    agent_root = Path(args.agent_root)
    if args.project_root:
        project_root = resolve_agent_path(agent_root, args.project_root)
    elif args.source is not None:
        project_root = find_lake_root(resolve_agent_path(agent_root, args.source))
    else:
        project_root = None
    logger.debug("Using project_root=%s", project_root)
    with _workspace_context(args.work_dir, agent_root=agent_root) as work_dir:
        check_workspace = build_check_workspace(args, agent_root=agent_root, project_root=project_root)
        logger.debug("Using attempt workspace: %s", work_dir)
        controller = ProofController(
            adapter=LeanAdapter(
                project_root=project_root,
                prefer_lake=not args.no_lake,
                disallow_sorry=not args.allow_sorry,
            ),
            action_generator=generator,
            workspace=AttemptWorkspace(work_dir),
            check_workspace=check_workspace,
            retriever=build_retriever(args),
            budget_config=BudgetConfig(
                max_checks=args.max_checks,
                max_model_calls=args.max_model_calls,
                per_check_timeout_seconds=args.lean_timeout,
                max_elapsed_seconds=args.max_elapsed_seconds,
            ),
            config=ControllerConfig(
                max_candidates_per_model_call=args.max_candidates,
                max_repair_rounds=args.max_repair_rounds,
                max_retrieval_results=args.max_retrieval_results,
                retrieve_before_first_model_call=args.retrieve_before_first_model_call,
            ),
        )
        try:
            result = controller.run(task)
        except ModelAdapterError as exc:
            logger.exception("Controller run failed during model call")
            print(json.dumps({"ok": False, "stage": "run", "error": str(exc)}, indent=2))
            return 2

    if args.trace_jsonl:
        trace_path = resolve_agent_path(agent_root, args.trace_jsonl)
        logger.info("Appending controller trace: %s", trace_path)
        JsonlTraceStore(trace_path, include_raw_output=args.trace_raw_output).append_result(result)

    logger.info(
        "CLI finished: task_id=%s accepted=%s stop_reason=%s attempts=%d",
        result.task.task_id,
        result.accepted,
        result.stop_reason,
        len(result.attempts),
    )
    print(json.dumps(_result_payload(result, include_candidate_file=True), indent=2))
    return 0 if result.accepted else 1


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


def apply_task_config(args: argparse.Namespace) -> argparse.Namespace:
    if not args.task_config:
        return args

    agent_root = resolve_agent_root(args.agent_root)
    config_path = resolve_agent_path(agent_root, args.task_config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"Task config must be a JSON object: {config_path}")
    setattr(args, "_task_config_data", config)
    setattr(args, "_task_config_path", str(config_path))

    aliases = {
        "retrieval_sources": "retrieval_source",
        "retrieval_source": "retrieval_source",
        "source": "source",
        "project_root": "project_root",
        "split": "split",
        "pattern": "pattern",
        "task_id": "task_id",
        "task_index": "task_index",
        "hole_marker": "hole_marker",
        "inactive_hole_fill": "inactive_hole_fill",
        "allow_multiple_marker_tasks": "allow_multiple_marker_tasks",
        "allow_multiple_sorry_tasks": "allow_multiple_sorry_tasks",
        "enable_retrieval": "enable_retrieval",
        "max_retrieval_results": "max_retrieval_results",
        "retrieve_before_first_model_call": "retrieve_before_first_model_call",
    }
    for key, target in aliases.items():
        if key not in config:
            continue
        current = getattr(args, target, _default_arg_value(target))
        value = config[key]
        if target == "retrieval_source":
            if current:
                continue
            if isinstance(value, str):
                setattr(args, target, [value])
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                setattr(args, target, value)
            else:
                raise ValueError("retrieval_source/retrieval_sources must be a string or list of strings.")
            continue
        if _is_default_arg_value(target, current):
            setattr(args, target, value)

    if args.source is None and not _config_has_inline_task_source(config):
        raise ValueError(f"Task config {config_path} does not define source, and no source was provided.")
    return args


def resolve_agent_root(value: str | Path | None) -> Path:
    return Path(value or ROOT).resolve()


def resolve_agent_path(agent_root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (agent_root / path).resolve()


def _require_source(args: argparse.Namespace) -> str:
    if args.source is None:
        raise ValueError("Provide a Lean source path or --task-config with a source field.")
    return args.source


def _config_has_inline_task_source(config: dict[str, Any]) -> bool:
    if any(isinstance(config.get(key), str) for key in ("proof_source", "source_template", "lean")):
        return True
    tasks = config.get("tasks")
    return isinstance(tasks, list) and any(
        isinstance(item, dict)
        and any(isinstance(item.get(key), str) for key in ("proof_source", "source_template", "lean"))
        for item in tasks
    )


def _is_default_arg_value(name: str, value: Any) -> bool:
    return value == _default_arg_value(name)


def _default_arg_value(name: str) -> Any:
    defaults: dict[str, Any] = {
        "source": None,
        "project_root": None,
        "split": None,
        "pattern": "*.lean",
        "task_id": None,
        "task_index": 0,
        "hole_marker": "{{proof}}",
        "inactive_hole_fill": "sorry",
        "allow_multiple_marker_tasks": False,
        "allow_multiple_sorry_tasks": False,
        "enable_retrieval": False,
        "max_retrieval_results": 5,
        "retrieve_before_first_model_call": False,
    }
    return defaults.get(name)


def build_tasks(args: argparse.Namespace) -> list[ProofTask]:
    config = TaskBuilderConfig(
        hole_marker=args.hole_marker,
        inactive_hole_fill=args.inactive_hole_fill,
        allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
        allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
    )
    builder = LeanTaskBuilder(config)
    task_config = getattr(args, "_task_config_data", None)
    if isinstance(task_config, dict) and _config_has_inline_task_source(task_config):
        tasks = _build_tasks_from_config(builder, args, task_config)
    else:
        source = resolve_agent_path(Path(args.agent_root), _require_source(args))
        if source.is_dir():
            tasks = builder.build_from_directory(source, split=args.split, pattern=args.pattern)
        else:
            tasks = builder.build_from_file(source, split=args.split)
    if not tasks:
        raise TaskBuildError("No tasks were extracted from task input.")
    return tasks


def _build_tasks_from_config(
    builder: LeanTaskBuilder,
    args: argparse.Namespace,
    config: dict[str, Any],
) -> list[ProofTask]:
    entries = config.get("tasks")
    if entries is None:
        entries = [config]
    if not isinstance(entries, list):
        raise ValueError("Task config field 'tasks' must be a list when provided.")

    tasks: list[ProofTask] = []
    defaults = config.get("metadata_defaults")
    if defaults is not None and not isinstance(defaults, dict):
        raise ValueError("Task config field 'metadata_defaults' must be an object when provided.")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("Each task config entry must be an object.")
        source_text = _inline_source_from_entry(entry)
        if source_text is None:
            continue
        metadata_defaults = dict(defaults or {})
        entry_metadata = entry.get("metadata")
        if entry_metadata is not None:
            if not isinstance(entry_metadata, dict):
                raise ValueError("Task config entry field 'metadata' must be an object.")
            metadata_defaults.update(entry_metadata)
        metadata_defaults.setdefault("task_config_file", getattr(args, "_task_config_path", None))
        metadata_defaults.setdefault("task_config_index", index)
        metadata_defaults.setdefault("task_source_kind", "inline")
        builder_with_metadata = LeanTaskBuilder(
            TaskBuilderConfig(
                hole_marker=args.hole_marker,
                inactive_hole_fill=args.inactive_hole_fill,
                default_split=args.split or builder.config.default_split,
                allowed_retrieval_scope=builder.config.allowed_retrieval_scope,
                metadata_defaults=metadata_defaults,
                allow_multiple_marker_tasks=args.allow_multiple_marker_tasks,
                allow_multiple_sorry_tasks=args.allow_multiple_sorry_tasks,
            )
        )
        entry_tasks = builder_with_metadata.build_from_source(
            source_text,
            source_path=entry.get("source_name") or entry.get("name") or f"task_config:{index}",
            split=entry.get("split") or args.split,
            task_id_prefix=entry.get("task_id_prefix") or entry.get("task_id") or entry.get("name"),
        )
        imports = _config_imports(config, entry)
        tasks.extend(_task_with_imports(task, imports) for task in entry_tasks)
    return tasks


def _inline_source_from_entry(entry: dict[str, Any]) -> str | None:
    for key in ("proof_source", "source_template", "lean"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def _config_imports(config: dict[str, Any], entry: dict[str, Any]) -> tuple[str, ...]:
    imports: list[str] = []
    for value in (config.get("imports"), entry.get("imports")):
        if value is None:
            continue
        if isinstance(value, str):
            imports.append(value)
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            imports.extend(value)
            continue
        raise ValueError("Task config imports must be a string or list of strings.")
    return tuple(dict.fromkeys(imports))


def _task_with_imports(task: ProofTask, imports: tuple[str, ...]) -> ProofTask:
    if not imports:
        return task
    return ProofTask(
        task_id=task.task_id,
        source_template=task.source_template,
        hole_marker=task.hole_marker,
        imports=tuple(dict.fromkeys((*task.imports, *imports))),
        metadata=task.metadata,
    )


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


def build_retriever(args: argparse.Namespace) -> LexicalLeanRetriever | None:
    if not args.enable_retrieval and not args.retrieval_source:
        return None
    agent_root = Path(args.agent_root)
    sources = args.retrieval_source or [_require_source(args)]
    sources = [resolve_agent_path(agent_root, source) for source in sources]
    logger.debug("Building lexical retriever from %d source path(s)", len(sources))
    return LexicalLeanRetriever.from_paths(sources)


def build_check_workspace(
    args: argparse.Namespace,
    *,
    agent_root: Path,
    project_root: Path | None,
) -> EphemeralCheckWorkspace | None:
    if args.no_lake or project_root is None:
        if args.check_work_dir:
            return EphemeralCheckWorkspace(
                resolve_agent_path(agent_root, args.check_work_dir),
                keep_files=args.keep_check_files,
            )
        return None
    if args.check_work_dir:
        check_root = Path(args.check_work_dir)
        if not check_root.is_absolute():
            check_root = project_root / check_root
    else:
        check_root = project_root / ".checks"
    return EphemeralCheckWorkspace(check_root, keep_files=args.keep_check_files)


def find_lake_root(source: str | Path) -> Path | None:
    path = Path(source).resolve()
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / "lakefile.lean").exists() or (candidate / "lakefile.toml").exists():
            return candidate
    return None


@contextmanager
def _workspace_context(
    work_dir: str | None,
    *,
    agent_root: Path = ROOT,
) -> Iterator[Path]:
    if work_dir is None:
        path = (agent_root / ".runs").resolve()
        path.mkdir(parents=True, exist_ok=True)
        yield path
        return
    path = resolve_agent_path(agent_root, work_dir)
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
