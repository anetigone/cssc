"""Final assembly and result construction for the structured controller.

When :func:`solution_tracker.has_complete_solution` reports a ready workspace,
the controller calls :func:`assemble_and_finalize` to reserve one additional
check budget, run the artifact assembler, and build the
:class:`ControllerResult`. Keeping this out of :mod:`.controller` shortens the
controller and isolates the assembly-specific dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent.proof_system.assembler import ArtifactAssembler
from agent.proof_system.workspace import WorkspaceStatus
from .run_state import _StructuredRunState, build_structured_result
from .solution_tracker import select_solution

if TYPE_CHECKING:
    from agent.proof_system.assembler import AssemblyResult
    from agent.proof_system.base import ProofSystemAdapter, ProofTask
    from agent.proof_system.workspace import ProofWorkspace
    from ..budget import BudgetManager
    from ..controller.types import ControllerResult
    from ..execution import ExecutionMode
    from ..safety import SafetyReviewer
    from agent.runtime.workspace import EphemeralCheckWorkspace


logger = logging.getLogger(__name__)


def assemble_and_finalize(
    task: ProofTask,
    workspace: ProofWorkspace,
    state: _StructuredRunState,
    *,
    budget: BudgetManager,
    adapter: ProofSystemAdapter,
    assembler: ArtifactAssembler,
    check_workspace: EphemeralCheckWorkspace | None,
    safety_reviewer: SafetyReviewer,
    execution_mode: ExecutionMode,
) -> ControllerResult:
    """Reserve budget, assemble the whole source, and build the run result.

    If the budget has no check remaining, returns an unaccepted result with
    ``stop_reason="budget:checks"``. Otherwise runs the assembler and returns
    either an accepted result or an assembly-failed result, both carrying the
    raw assembly outcome in ``metadata["assembly"]``.
    """
    if not budget.can_check():
        state.stop_reason = "budget:checks"
        return build_structured_result(
            state,
            task,
            workspace,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=execution_mode,
            budget=budget,
            safety_reviewer=safety_reviewer,
        )
    budget_slice = budget.reserve_check()
    solution_branches = select_solution(workspace)
    artifacts = {
        branch.obligation_id: branch.lean_artifact
        for branch in solution_branches
        if branch.lean_artifact is not None
    }
    assembly = assembler.assemble(
        workspace,
        artifacts,
        adapter=adapter,
        task=task,
        check_workspace=check_workspace,
        budget_slice=budget_slice,
        safety_reviewer=safety_reviewer,
    )
    logger.info(
        "Structured assembly: task_id=%s accepted=%s errors=%d",
        task.task_id,
        assembly.accepted,
        len(assembly.errors),
    )
    if assembly.accepted:
        workspace = workspace.successor(status=WorkspaceStatus.ACCEPTED)
        return build_structured_result(
            state,
            task,
            workspace,
            accepted=True,
            stop_reason="accepted",
            execution_mode=execution_mode,
            budget=budget,
            safety_reviewer=safety_reviewer,
            assembly_outcome=assembly,
        )
    state.stop_reason = "assembly_failed"
    return build_structured_result(
        state,
        task,
        workspace,
        accepted=False,
        stop_reason=state.stop_reason,
        execution_mode=execution_mode,
        budget=budget,
        safety_reviewer=safety_reviewer,
        assembly_outcome=assembly,
    )
