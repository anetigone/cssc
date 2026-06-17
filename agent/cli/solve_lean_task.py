"""Build Lean proof tasks from files and solve one selected task.

Examples:
    python solve_lean_task.py lean_workspace/Cssc/Tasks/Basic.lean --list-tasks
    python solve_lean_task.py Basic.lean --task-index 0 --candidate trivial
    python solve_lean_task.py Basic.lean --use-model

Note:
    When the input is natural-language, the formalizer validates its generated
    scaffold against Lean before returning tasks. Both the proof-search adapter
    and the scaffold-validation adapter use the persistent Lean server unless
    ``--no-lean-server`` is set. The validation adapter tolerates ``sorry``
    placeholders so the scaffold's declaration/import shape can be checked
    before proof search fills the hole.
"""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from agent import (
    BudgetConfig,
    ControllerConfig,
    EphemeralCheckWorkspace,
    JsonlTraceStore,
    LeanAdapter,
    ModelAdapterError,
    ProofController,
    TaskBuildError,
    TaskInputKind,
)
from agent.input.validation import LeanAdapterScaffoldChecker, ValidationConfig
from agent.runtime.logging_config import configure_logging
from agent.runtime.workspace import AttemptWorkspace

from .config import apply_task_config
from .generators import build_action_generator, build_formalization_agent, build_retriever
from .output import result_payload, task_summary
from .parser import build_parser
from .paths import find_lake_root, resolve_agent_path, resolve_agent_root
from .tasks import classify_input, build_tasks, select_task
from .workspace import _workspace_context, build_check_workspace


logger = logging.getLogger(__name__)


@dataclass
class _LeanServices:
    """Holds the Lean adapters used during a CLI run and cleans them up."""

    adapter: LeanAdapter
    validation_adapter: LeanAdapter

    def close(self) -> None:
        self.validation_adapter.close()
        self.adapter.close()


def _run_artifact_path(agent_root: Path, value: str | None, run_name: str | None) -> Path | None:
    """Resolve a run artifact path, optionally grouping under ``.runs/<run_name>``.

    When ``run_name`` is set, artifacts are written into
    ``AGENT_ROOT/.runs/<run_name>/<basename>`` so a single run's log and trace
    land in the same directory regardless of their file names. Without it the
    value is resolved verbatim.
    """
    if not value:
        return None
    path = resolve_agent_path(agent_root, value)
    if run_name:
        runs_root = (agent_root / ".runs").resolve()
        if path.parent.resolve() == runs_root or path.parent.resolve() == agent_root.resolve():
            return runs_root / _safe_name(run_name) / path.name
    return path


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned or "run"


def _resolve_formalization_cache_dir(args: Namespace) -> str | None:
    """Reconcile the formalization-cache flags into a single directory or None."""
    if args.no_formalization_cache:
        return None
    if args.formalization_cache_dir:
        return args.formalization_cache_dir
    if args.formalization_cache:
        return ".runs/formalization_cache"
    return None


@contextmanager
def _lean_services(
    args: Namespace,
    project_root: Path | None,
) -> Iterator[_LeanServices]:
    """Create Lean adapters for the run and ensure they are closed.

    Both adapters can use the persistent Lean server. The validation adapter
    tolerates ``sorry`` placeholders because scaffold validation checks the
    generated declaration/import shape before proof search fills the hole.
    """
    kwargs = {
        "project_root": project_root,
        "prefer_lake": not args.no_lake,
    }
    services = _LeanServices(
        adapter=LeanAdapter(
            **kwargs,
            disallow_sorry=not args.allow_sorry,
            use_server=not args.no_lean_server,
        ),
        validation_adapter=LeanAdapter(
            **kwargs,
            disallow_sorry=False,
            use_server=not args.no_lean_server,
        ),
    )
    try:
        yield services
    finally:
        services.close()


def _build_scaffold_checker(
    args: Namespace,
    services: _LeanServices,
    task_config: Any,
    check_workspace: EphemeralCheckWorkspace | None,
) -> LeanAdapterScaffoldChecker | None:
    """Build a scaffold checker only when the input is natural language."""
    if classify_input(args, task_config) != TaskInputKind.NATURAL_LANGUAGE:
        return None
    scaffold_timeout = args.scaffold_timeout
    if scaffold_timeout is None:
        scaffold_timeout = args.lean_timeout
    return LeanAdapterScaffoldChecker(
        services.validation_adapter,
        check_workspace,
        validation=ValidationConfig(check_timeout_seconds=scaffold_timeout),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args = apply_task_config(args)
        agent_root = resolve_agent_root(args.agent_root)
        args.agent_root = str(agent_root)
        args.formalization_cache_dir = _resolve_formalization_cache_dir(args)
        if args.log_file:
            args.log_file = str(_run_artifact_path(agent_root, args.log_file, args.run_name))
        if args.trace_jsonl:
            args.trace_jsonl = str(_run_artifact_path(agent_root, args.trace_jsonl, args.run_name))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "stage": "task_config", "error": str(exc)}, indent=2))
        return 2

    try:
        configure_logging(level=args.log_level, log_file=args.log_file)
    except ValueError as exc:
        print(json.dumps({"ok": False, "stage": "logging_config", "error": str(exc)}, indent=2))
        return 2

    logger.info("CLI started: source=%s task_config=%s use_model=%s", args.source, args.task_config, args.use_model)

    agent_root = Path(args.agent_root)
    if args.project_root:
        project_root = resolve_agent_path(agent_root, args.project_root)
    elif args.source is not None:
        project_root = find_lake_root(resolve_agent_path(agent_root, args.source))
    else:
        project_root = None
    logger.debug("Using project_root=%s", project_root)

    with (
        _workspace_context(args.work_dir, agent_root=agent_root) as work_dir,
        _lean_services(args, project_root) as services,
    ):
        try:
            task_config = getattr(args, "_task_config_data", None)
            check_workspace = build_check_workspace(
                args, agent_root=agent_root, project_root=project_root
            )
            checker = _build_scaffold_checker(args, services, task_config, check_workspace)

            formalizer = build_formalization_agent(args, checker=checker)
            tasks = build_tasks(args, formalizer=formalizer)
            logger.info("Built %d task(s) from task input", len(tasks))

            if args.list_tasks:
                payload = {"tasks": [task_summary(task, index) for index, task in enumerate(tasks)]}
                print(json.dumps(payload, indent=2))
                return 0

            task = select_task(tasks, task_id=args.task_id, task_index=args.task_index)
            logger.info("Selected task: task_id=%s", task.task_id)
            generator = build_action_generator(args)

            logger.debug("Using attempt workspace: %s", work_dir)
            controller = ProofController(
                adapter=services.adapter,
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

        except (TaskBuildError, ValueError, ModelAdapterError) as exc:
            logger.exception("CLI setup failed")
            print(json.dumps({"ok": False, "stage": "setup", "error": str(exc)}, indent=2))
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
