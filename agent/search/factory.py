"""Select and construct the controller for a given execution mode.

This is the single decision point that maps an :class:`ExecutionMode` to a
concrete executor. Because the mode is decided here at startup and the chosen
controller never reads it for control flow, no runtime code path can switch
modes mid-run.
"""

from __future__ import annotations

from typing import Any

from .execution import ExecutionMode


class StructuredModeUnavailableError(NotImplementedError):
    """Historically raised when structured mode had no executor.

    Phase 6 ships the structured executor (:class:`StructuredController`), so
    ``build_controller`` no longer raises this for a valid structured request.
    The class is kept for backward compatibility: ``agent.cli.app`` and any
    external callers still import it, and it is raised if the structured
    executor is requested with a mismatched (non-STRUCTURED) config.
    """


def _check_config_mode(execution_mode: ExecutionMode, config: Any) -> None:
    """Reject a config whose execution_mode disagrees with the factory choice.

    Keeping a single controller config object is how the trace records the
    common observation field; if the caller hands a minimal config to a
    structured request (or vice versa) the two would disagree on the recorded
    mode, so fail loudly.
    """
    if config is None:
        return
    configured_mode = getattr(config, "execution_mode", execution_mode)
    if configured_mode != execution_mode:
        raise ValueError(
            "Controller config execution_mode does not match the mode "
            f"selected by the factory: {configured_mode!s} != {execution_mode!s}."
        )


def build_controller(
    execution_mode: ExecutionMode,
    *,
    adapter: Any,
    action_generator: Any,
    workspace: Any,
    check_workspace: Any = None,
    retriever: Any = None,
    context_summarizer: Any = None,
    budget_config: Any = None,
    config: Any = None,
    safety_reviewer: Any = None,
    cost_estimator: Any = None,
    model_router_config: Any = None,
    action_runtime_config: Any = None,
) -> Any:
    """Return the controller for ``execution_mode``.

    Both modes are implemented: ``MINIMAL`` returns the linear
    :class:`ProofController`, ``STRUCTURED`` returns the frontier-driven
    :class:`StructuredController`. Both reject a config whose execution_mode
    disagrees with the requested mode.
    """
    _check_config_mode(execution_mode, config)

    if execution_mode is ExecutionMode.MINIMAL:
        # Lazy import avoids a factory -> controller -> metrics -> execution
        # import chain being eagerly pulled when only the enum is needed.
        from .controller import ProofController

        return ProofController(
            adapter=adapter,
            action_generator=action_generator,
            workspace=workspace,
            check_workspace=check_workspace,
            retriever=retriever,
            context_summarizer=context_summarizer,
            budget_config=budget_config,
            config=config,
            safety_reviewer=safety_reviewer,
        )

    if execution_mode is ExecutionMode.STRUCTURED:
        # Lazy import keeps the minimal import graph free of the structured
        # package: importing the enum (or this factory) never pulls the
        # workspace / frontier / reducer modules.
        from .structured import StructuredController

        return StructuredController(
            adapter=adapter,
            action_generator=action_generator,
            workspace=workspace,
            check_workspace=check_workspace,
            retriever=retriever,
            context_summarizer=context_summarizer,
            budget_config=budget_config,
            config=config,
            safety_reviewer=safety_reviewer,
            cost_estimator=cost_estimator,
            model_router_config=model_router_config,
            action_runtime_config=action_runtime_config,
        )

    raise StructuredModeUnavailableError(
        f"unknown execution mode {execution_mode!s}"
    )
