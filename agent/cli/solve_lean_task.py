"""Build Lean proof tasks from files and solve one selected task.

Examples:
    python solve_lean_task.py lean_workspace/Cssc/Tasks/Basic.lean --list-tasks
    python solve_lean_task.py Basic.lean --task-index 0 --candidate trivial
    python solve_lean_task.py Basic.lean --use-model
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent import (
    BudgetConfig,
    ControllerConfig,
    JsonlTraceStore,
    LeanAdapter,
    ModelAdapterError,
    ProofController,
    TaskBuildError,
)
from agent.runtime.logging_config import configure_logging
from agent.runtime.workspace import AttemptWorkspace

from .config import apply_task_config
from .generators import build_action_generator, build_retriever
from .output import result_payload, task_summary
from .parser import build_parser
from .paths import find_lake_root, resolve_agent_path, resolve_agent_root
from .tasks import build_tasks, select_task
from .workspace import _workspace_context, build_check_workspace


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
            payload = {"tasks": [task_summary(task, index) for index, task in enumerate(tasks)]}
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
    print(json.dumps(result_payload(result, include_candidate_file=True), indent=2))
    return 0 if result.accepted else 1
