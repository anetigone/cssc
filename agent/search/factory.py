"""Select and construct the controller for a given execution mode.

This is the single decision point that maps an :class:`ExecutionMode` to a
concrete executor. Because the mode is decided here at startup and the chosen
controller never reads it for control flow in Phase 2, no runtime code path
can switch modes mid-run.
"""

from __future__ import annotations

from typing import Any

from .execution import ExecutionMode


class StructuredModeUnavailableError(NotImplementedError):
    """Raised when the structured executor is requested before Phase 3.

    Phase 2 only parameterizes the two modes; the structured executor
    (``ProofWorkspace`` / ``ObligationGraph`` / frontier) lands in Phase 3+.
    Selecting ``--execution-mode structured`` must fail loudly rather than
    silently behave like minimal, so experiments never record structured runs
    that were actually minimal.
    """


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
) -> Any:
    """Return the controller for ``execution_mode``.

    For Phase 2 only ``MINIMAL`` is implemented. ``STRUCTURED`` raises
    :class:`StructuredModeUnavailableError`.
    """
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
    raise StructuredModeUnavailableError(
        "structured execution mode is not implemented until Phase 3 "
        "(ProofWorkspace / ObligationGraph / frontier). "
        "Re-run with --execution-mode minimal."
    )
