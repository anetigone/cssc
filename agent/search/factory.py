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
    """Raised when the structured executor is requested before its frontier lands.

    Phase 3 introduced the structured state primitives (``ProofWorkspace`` /
    ``ProofObligation`` / ``ObligationGraph`` and the ``ArtifactAssembler``)
    as pure data plus a final-assembly whole-recheck, but the structured
    *executor* — the frontier / AND-OR search that drives them end to end — is
    Phase 4-6. Selecting ``--execution-mode structured`` must fail loudly
    rather than silently behave like minimal, so experiments never record
    structured runs that were actually minimal.
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
        if config is not None:
            configured_mode = getattr(config, "execution_mode", execution_mode)
            if configured_mode != execution_mode:
                raise ValueError(
                    "Controller config execution_mode does not match the mode "
                    f"selected by the factory: {configured_mode!s} != {execution_mode!s}."
                )
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
        "the structured executor (frontier / AND-OR search) is not implemented "
        "until Phase 4-6. Phase 3 ships the structured state primitives "
        "(ProofWorkspace / ObligationGraph / ArtifactAssembler) but no driver. "
        "Re-run with --execution-mode minimal."
    )
