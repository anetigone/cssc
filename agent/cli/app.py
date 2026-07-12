"""Build Lean proof tasks from files and solve one selected task.

Examples:
    python app.py lean_workspace/Cssc/Tasks/Basic.lean --list-tasks
    python app.py Basic.lean --task-index 0 --candidate trivial
    python app.py Basic.lean --use-model

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
import sys
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from agent import (
    BudgetConfig,
    ControllerConfig,
    EphemeralCheckWorkspace,
    ExecutionMode,
    JsonlTraceStore,
    LeanAdapter,
    ModelAdapterError,
    StructuredModeUnavailableError,
    TaskBuildError,
    TaskInputKind,
    build_controller,
)
from agent.agents import FormalizationRequest
from agent.input.normalizer import InputNormalizer
from agent.input.validation import LeanAdapterScaffoldChecker, ValidationConfig
from agent.runtime.logging_config import configure_logging
from agent.runtime.workspace import AttemptWorkspace

from .config import apply_task_config
from .artifacts import formalization_artifact, formalization_payload
from .generators import (
    build_action_generator,
    build_context_summarizer,
    build_formalization_agent,
    build_retriever,
)
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
            server_startup_timeout_seconds=getattr(args, "lean_server_startup_timeout", 60.0),
            server_fallback_seconds=getattr(args, "lean_server_fallback_seconds", 2.0),
        ),
        validation_adapter=LeanAdapter(
            **kwargs,
            disallow_sorry=False,
            use_server=not args.no_lean_server,
            server_startup_timeout_seconds=getattr(args, "lean_server_startup_timeout", 60.0),
            server_fallback_seconds=getattr(args, "lean_server_fallback_seconds", 2.0),
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
    if getattr(args, "no_check", False) or classify_input(args, task_config) != TaskInputKind.NATURAL_LANGUAGE:
        return None
    scaffold_timeout = args.scaffold_timeout
    if scaffold_timeout is None:
        scaffold_timeout = args.lean_timeout
    return LeanAdapterScaffoldChecker(
        services.validation_adapter,
        check_workspace,
        validation=ValidationConfig(check_timeout_seconds=scaffold_timeout),
    )


def _fail(stage: str, exc: BaseException) -> int:
    print(json.dumps({"ok": False, "stage": stage, "error": str(exc)}, indent=2))
    return 2


def _normalize_input(args: Namespace) -> Any:
    """Normalize raw input into natural-language/Lean specs (no task building)."""
    return InputNormalizer().normalize(
        source=getattr(args, "source", None),
        problem=getattr(args, "problem", None),
        problem_file=getattr(args, "problem_file", None),
        input_kind=getattr(args, "input_kind", "auto"),
        task_config=getattr(args, "_task_config_data", None),
        task_config_path=getattr(args, "_task_config_path", None),
        agent_root=args.agent_root,
        pattern=args.pattern,
        split=args.split,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    defaults = parser.parse_args([args.command])
    args._cli_fields = {
        key
        for key, value in vars(args).items()
        if key != "command" and value != getattr(defaults, key, None)
    }
    try:
        _promote_positional_artifact(args)
        args = apply_task_config(args)
        agent_root = resolve_agent_root(args.agent_root)
        args.agent_root = str(agent_root)
        if hasattr(args, "formalization_cache_dir"):
            args.formalization_cache_dir = _resolve_formalization_cache_dir(args)
        if getattr(args, "log_file", None):
            args.log_file = str(_run_artifact_path(agent_root, args.log_file, args.run_name))
        if getattr(args, "trace_jsonl", None):
            args.trace_jsonl = str(_run_artifact_path(agent_root, args.trace_jsonl, args.run_name))
    except (OSError, ValueError) as exc:
        return _fail("task_config", exc)

    try:
        configure_logging(level=args.log_level, log_file=args.log_file)
    except ValueError as exc:
        return _fail("logging_config", exc)

    command = args.command
    logger.info(
        "CLI started: command=%s source=%s task_config=%s use_model=%s",
        command,
        args.source,
        args.task_config,
        args.use_model,
    )

    agent_root = Path(args.agent_root)
    if args.project_root:
        project_root = resolve_agent_path(agent_root, args.project_root)
    elif args.source is not None:
        project_root = find_lake_root(resolve_agent_path(agent_root, args.source))
    else:
        project_root = None
    logger.debug("Using project_root=%s", project_root)

    if command == "formalize":
        return run_formalize(args, agent_root=agent_root, project_root=project_root)
    if command == "prove":
        return run_prove(args, agent_root=agent_root, project_root=project_root)
    return run_solve(args, agent_root=agent_root, project_root=project_root)


def _promote_positional_artifact(args: Namespace) -> None:
    """Treat a positional JSON input to ``prove`` as its task config."""
    source = getattr(args, "source", None)
    if args.command == "prove" and not args.task_config and source and Path(source).suffix.lower() == ".json":
        args.task_config = source
        args.source = None


def run_solve(args: Namespace, *, agent_root: Path, project_root: Path | None) -> int:
    result: Any = None
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

            formalizer = build_formalization_agent(args, checker=checker, project_root=project_root)
            tasks = build_tasks(args, formalizer=formalizer)
            logger.info("Built %d task(s) from task input", len(tasks))

            if args.list_tasks:
                payload = {"tasks": [task_summary(task, index) for index, task in enumerate(tasks)]}
                print(json.dumps(payload, indent=2))
                return 0

            task = select_task(tasks, task_id=args.task_id, task_index=args.task_index)
            logger.info("Selected task: task_id=%s", task.task_id)
            logger.debug("Using attempt workspace: %s", work_dir)
            result = _run_controller(args, task, services, work_dir, check_workspace, project_root)

        except (TaskBuildError, ValueError, ModelAdapterError) as exc:
            logger.debug("CLI setup failed", exc_info=True)
            return _fail("setup", exc)
        except StructuredModeUnavailableError as exc:
            logger.debug("Execution mode unavailable: %s", exc)
            return _fail("execution_mode", exc)
        except Exception:
            # The controller / Lean server may raise something we don't model
            # explicitly (subprocess crash, OSError, ...). Surface it instead of
            # masking the real error with an UnboundLocalError on ``result``.
            logger.exception("solve failed unexpectedly")
            return _fail("solve", _UnexpectedError())

    return _finalize_run(args, result, agent_root, stage="solve")


def run_prove(args: Namespace, *, agent_root: Path, project_root: Path | None) -> int:
    """Run proof search without invoking the formalizer."""
    task_config = getattr(args, "_task_config_data", None)
    if classify_input(args, task_config) != TaskInputKind.LEAN:
        return _fail("input_kind", ValueError("prove requires a Lean source or formalization artifact."))

    result: Any = None
    with (
        _workspace_context(args.work_dir, agent_root=agent_root) as work_dir,
        _lean_services(args, project_root) as services,
    ):
        try:
            check_workspace = build_check_workspace(args, agent_root=agent_root, project_root=project_root)
            tasks = build_tasks(args, formalizer=None)
            if args.list_tasks:
                _emit_payload(
                    args,
                    {"tasks": [task_summary(task, index) for index, task in enumerate(tasks)]},
                    agent_root,
                )
                return 0
            task = select_task(tasks, task_id=args.task_id, task_index=args.task_index)
            result = _run_controller(args, task, services, work_dir, check_workspace, project_root)
        except (TaskBuildError, ValueError, ModelAdapterError) as exc:
            logger.debug("prove failed", exc_info=True)
            return _fail("prove", exc)
        except StructuredModeUnavailableError as exc:
            logger.debug("Execution mode unavailable: %s", exc)
            return _fail("execution_mode", exc)
        except Exception:
            logger.exception("prove failed unexpectedly")
            return _fail("prove", _UnexpectedError())

    return _finalize_run(args, result, agent_root, stage="prove")


def _finalize_run(args: Namespace, result: Any, agent_root: Path, *, stage: str) -> int:
    """Emit trace + payload for a completed run. Only called when result is set."""
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
    payload = result_payload(result, include_candidate_file=True)
    payload["stage"] = stage
    _emit_payload(args, payload, agent_root)
    return 0 if result.accepted else 1


class _UnexpectedError(RuntimeError):
    """Placeholder exception for unmapped controller failures."""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return "Unexpected error during proof search; see logs for details."



def _run_controller(
    args: Namespace,
    task: Any,
    services: _LeanServices,
    work_dir: Path,
    check_workspace: EphemeralCheckWorkspace | None,
    project_root: Path | None,
) -> Any:
    generator = build_action_generator(args, project_root=project_root)
    execution_mode = ExecutionMode(args.execution_mode)
    model_router_config = None
    action_runtime_config = None
    cost_estimator = None
    if execution_mode is ExecutionMode.STRUCTURED:
        from agent.search.structured.model_router import ModelRouterConfig
        from agent.search.structured.action_runtime_config import (
            ActionCostSource,
            ActionRuntimeConfig,
        )
        from agent.search.structured.cost_estimator import (
            ActionCostEstimator,
            cost_history_snapshot_from_dict,
        )
        import json

        model_router_config = ModelRouterConfig(
            enabled=bool(getattr(args, "enable_model_routing", False)),
            cheap_model=(
                getattr(args, "proof_model", None)
                or getattr(args, "model", None)
            ),
            strong_model=getattr(args, "strong_proof_model", None),
        )
        action_runtime_config = ActionRuntimeConfig(
            cost_source=ActionCostSource(getattr(args, "action_cost_source", "auto")),
            remaining_budget_policy=bool(
                getattr(args, "remaining_budget_policy", True)
            ),
        )
        snapshot_arg = getattr(args, "cost_history_snapshot", None)
        if snapshot_arg:
            snapshot_path = resolve_agent_path(Path.cwd(), snapshot_arg)
            snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            cost_estimator = ActionCostEstimator(
                cost_history_snapshot_from_dict(snapshot_data)
            )
    controller = build_controller(
        execution_mode,
        adapter=services.adapter,
        action_generator=generator,
        workspace=AttemptWorkspace(work_dir),
        check_workspace=check_workspace,
        retriever=build_retriever(args),
        context_summarizer=build_context_summarizer(args),
        budget_config=BudgetConfig(
            max_checks=args.max_checks,
            max_model_calls=args.max_model_calls,
            per_check_timeout_seconds=args.lean_timeout,
            max_elapsed_seconds=args.max_elapsed_seconds,
        ),
        config=ControllerConfig(
            max_candidates_per_model_call=args.max_candidates,
            max_retrieval_results=args.max_retrieval_results,
            retrieve_before_first_model_call=args.retrieve_before_first_model_call,
            execution_mode=execution_mode,
            frontier_policy=getattr(args, "frontier_policy", "legacy"),
        ),
        model_router_config=model_router_config,
        cost_estimator=cost_estimator,
        action_runtime_config=action_runtime_config,
    )
    return controller.run(task)


def run_formalize(args: Namespace, *, agent_root: Path, project_root: Path | None) -> int:
    """Run only the formalizer and print the validated Lean scaffold(s)."""
    task_config = getattr(args, "_task_config_data", None)
    if classify_input(args, task_config) != TaskInputKind.NATURAL_LANGUAGE:
        return _fail(
            "input_kind",
            ValueError("formalize requires natural-language input; use 'solve' for Lean sources."),
        )

    try:
        normalized = _normalize_input(args)
        if args.list_tasks:
            payload = {
                "tasks": [
                    {
                        "index": index,
                        "task_id": spec.task_id,
                        "imports": list(spec.imports),
                        "problem_excerpt": spec.text[:200],
                    }
                    for index, spec in enumerate(normalized.specs)
                ]
            }
            _emit_payload(args, payload, agent_root)
            return 0
        specs = _select_specs(
            normalized.specs,
            task_id=args.task_id,
            task_index=args.task_index,
            all_tasks=getattr(args, "all_tasks", False),
        )
        if getattr(args, "no_check", False):
            results = _formalize_specs(args, specs, checker=None, project_root=project_root)
        else:
            with _lean_services(args, project_root) as services:
                check_workspace = build_check_workspace(args, agent_root=agent_root, project_root=project_root)
                checker = _build_scaffold_checker(args, services, task_config, check_workspace)
                results = _formalize_specs(args, specs, checker=checker, project_root=project_root)
    except (TaskBuildError, ValueError, ModelAdapterError) as exc:
        logger.debug("formalize failed", exc_info=True)
        return _fail("formalize", exc)

    payload = formalization_payload(results)
    _emit_payload(args, payload, agent_root)
    return 0


def _select_specs(specs: tuple[Any, ...], *, task_id: str | None, task_index: int, all_tasks: bool) -> list[Any]:
    if all_tasks:
        return list(specs)
    if task_id is not None:
        for spec in specs:
            if spec.task_id == task_id:
                return [spec]
        raise ValueError(f"Task id not found: {task_id}")
    if task_index < 0 or task_index >= len(specs):
        raise ValueError(f"Task index {task_index} is out of range for {len(specs)} tasks.")
    return [specs[task_index]]


def _formalize_specs(args: Namespace, specs: list[Any], *, checker: Any, project_root: Path | None) -> list[dict[str, Any]]:
    formalizer = build_formalization_agent(args, checker=checker, project_root=project_root)
    if formalizer is None:
        raise ValueError("formalize requires natural-language input.")
    artifacts: list[dict[str, Any]] = []
    for spec in specs:
        outcome = formalizer.formalize(
            FormalizationRequest(
                problem=spec.text,
                task_id=spec.task_id,
                imports=spec.imports,
                informal_proof=spec.informal_proof,
                context=spec.context,
                hole_marker=args.hole_marker,
                metadata=spec.metadata,
            )
        )
        artifacts.append(formalization_artifact(spec, outcome, hole_marker=args.hole_marker))
    return artifacts


def _emit_payload(args: Namespace, payload: dict[str, Any], agent_root: Path) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    output = getattr(args, "output", None)
    if output:
        path = resolve_agent_path(agent_root, output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    # Allow ``python -m agent.cli.app`` as well as ``python -m agent.cli``.
    raise SystemExit(main())
