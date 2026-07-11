"""Final assembly and result construction for the structured controller."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent.proof_system.assembler import ArtifactAssembler
from agent.proof_system.workspace import WorkspaceStatus
from agent.search.cost_ledger import (
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)
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
    frontier_policy: str = "legacy",
) -> ControllerResult:
    """Reserve budget, assemble the whole source, and build the run result."""
    if not budget.can_check():
        state.stop_reason = "budget:checks"
        logger.info(
            "Structured assembly skipped due to check budget: task_id=%s workspace_version=%d",
            task.task_id,
            workspace.version,
        )
        return build_structured_result(
            state,
            task,
            workspace,
            accepted=False,
            stop_reason=state.stop_reason,
            execution_mode=execution_mode,
            budget=budget,
            safety_reviewer=safety_reviewer,
            frontier_policy=frontier_policy,
        )
    budget_slice = budget.reserve_check()
    solution_branches = select_solution(workspace)
    artifacts = {
        branch.obligation_id: branch.lean_artifact
        for branch in solution_branches
        if branch.lean_artifact is not None
    }
    logger.info(
        "Structured assembly starting: task_id=%s workspace_version=%d branches=%d artifacts=%d",
        task.task_id,
        workspace.version,
        len(solution_branches),
        len(artifacts),
    )
    logger.debug(
        "Structured assembly artifact obligations: task_id=%s obligations=%s",
        task.task_id,
        sorted(artifacts),
    )
    assembly = assembler.assemble(
        workspace,
        artifacts,
        adapter=adapter,
        task=task,
        check_workspace=check_workspace,
        budget_slice=budget_slice,
        safety_reviewer=safety_reviewer,
    )
    if assembly.check_result is not None:
        state.cost_ledger = state.cost_ledger.append(CostLedgerEvent(
            event_id=f"checker:{len(state.cost_ledger.events)}",
            kind=CostLedgerEventKind.CHECKER,
            scope=CostScope.ASSEMBLY,
            status="completed",
            attempt_index=state.attempt_index,
            checker_kind="assembly",
            category=assembly.check_result.category.value,
            wall_time_ms=CostMeasurement.observed(
                assembly.check_result.elapsed_seconds * 1000
            ),
            cpu_time_ms=CostMeasurement.unavailable(
                "checker CPU time not reported"
            ),
            metadata={"action_id": "assembly"},
        ))
    logger.info(
        "Structured assembly: task_id=%s accepted=%s errors=%d",
        task.task_id,
        assembly.accepted,
        len(assembly.errors),
    )
    if assembly.accepted:
        workspace = workspace.successor(status=WorkspaceStatus.ACCEPTED)
        result = build_structured_result(
            state,
            task,
            workspace,
            accepted=True,
            stop_reason="accepted",
            execution_mode=execution_mode,
            budget=budget,
            safety_reviewer=safety_reviewer,
            assembly_outcome=assembly,
            frontier_policy=frontier_policy,
        )
        logger.info(
            "Structured controller run finished: task_id=%s accepted=%s stop_reason=%s "
            "attempts=%d checks=%d model_calls=%d",
            task.task_id,
            result.accepted,
            result.stop_reason,
            len(result.attempts),
            result.budget.checks_used,
            result.budget.model_calls_used,
        )
        return result
    state.stop_reason = "assembly_failed"
    result = build_structured_result(
        state,
        task,
        workspace,
        accepted=False,
        stop_reason=state.stop_reason,
        execution_mode=execution_mode,
        budget=budget,
        safety_reviewer=safety_reviewer,
        assembly_outcome=assembly,
        frontier_policy=frontier_policy,
    )
    logger.info(
        "Structured controller run finished: task_id=%s accepted=%s stop_reason=%s "
        "attempts=%d checks=%d model_calls=%d",
        task.task_id,
        result.accepted,
        result.stop_reason,
        len(result.attempts),
        result.budget.checks_used,
        result.budget.model_calls_used,
    )
    return result
