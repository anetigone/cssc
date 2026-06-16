"""Minimal controller loop for running proof attempts end to end."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .action import ActionGenerationRequest, ActionGenerator
from .budget import BudgetConfig, BudgetManager, BudgetSnapshot
from ..proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProofSystemAdapter,
    ProofTask,
)
from ..runtime.workspace import AttemptWorkspace


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControllerConfig:
    """Small policy knobs for the MVP controller."""

    max_candidates_per_model_call: int = 1
    candidate_extension: str = ".lean"
    stop_on_tool_unavailable: bool = True


@dataclass(frozen=True)
class AttemptRecord:
    """One generated candidate and its checker result."""

    attempt_index: int
    candidate_id: str
    edit: CandidateEdit
    candidate_file: Path
    check_result: CheckResult


@dataclass(frozen=True)
class ControllerResult:
    """Final outcome of one controller run."""

    task: ProofTask
    accepted: bool
    attempts: tuple[AttemptRecord, ...]
    budget: BudgetSnapshot
    stop_reason: str
    accepted_attempt: AttemptRecord | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProofController:
    """Coordinate action generation, rendering, materialization, and checking."""

    def __init__(
        self,
        *,
        adapter: ProofSystemAdapter,
        action_generator: ActionGenerator,
        workspace: AttemptWorkspace,
        budget_config: BudgetConfig | None = None,
        config: ControllerConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.action_generator = action_generator
        self.workspace = workspace
        self.budget = BudgetManager(budget_config)
        self.config = config or ControllerConfig()

    def run(self, task: ProofTask) -> ControllerResult:
        logger.info("Controller run started: task_id=%s", task.task_id)
        attempts: list[AttemptRecord] = []
        feedback_history: list[ParsedFeedback] = []
        stop_reason = "budget"
        attempt_index = 0

        while self.budget.can_call_model() and self.budget.can_check():
            self.budget.reserve_model_call()
            logger.debug(
                "Generating actions: task_id=%s attempt_index=%d previous_feedback=%d",
                task.task_id,
                attempt_index,
                len(feedback_history),
            )
            request = ActionGenerationRequest(
                task=task,
                attempt_index=attempt_index,
                previous_feedback=tuple(feedback_history),
                max_candidates=self.config.max_candidates_per_model_call,
            )
            actions = tuple(self.action_generator.generate(request))
            logger.debug(
                "Generated %d action(s): task_id=%s attempt_index=%d",
                len(actions),
                task.task_id,
                attempt_index,
            )
            if not actions:
                stop_reason = "no_actions"
                logger.info("Controller stopped: task_id=%s reason=%s", task.task_id, stop_reason)
                break

            for action in actions[: self.config.max_candidates_per_model_call]:
                if not self.budget.can_check():
                    stop_reason = "budget"
                    logger.info("Controller stopped before check: task_id=%s reason=%s", task.task_id, stop_reason)
                    break

                edit = action.to_edit()
                logger.debug(
                    "Rendering candidate: task_id=%s attempt_index=%d action=%s",
                    task.task_id,
                    attempt_index,
                    edit.action,
                )
                source = self.adapter.render_candidate(task, edit)
                materialized = self.workspace.write_candidate(
                    task,
                    edit,
                    source,
                    extension=self.config.candidate_extension,
                )
                budget_slice = self.budget.reserve_check()
                logger.debug(
                    "Checking candidate: task_id=%s attempt_index=%d candidate_id=%s path=%s timeout=%s",
                    task.task_id,
                    attempt_index,
                    materialized.candidate_id,
                    materialized.path,
                    budget_slice.timeout_seconds,
                )
                check_result = self.adapter.check(materialized.path, budget_slice)
                record = AttemptRecord(
                    attempt_index=attempt_index,
                    candidate_id=materialized.candidate_id,
                    edit=edit,
                    candidate_file=materialized.path,
                    check_result=check_result,
                )
                attempts.append(record)
                attempt_index += 1
                logger.info(
                    "Candidate checked: task_id=%s attempt_index=%d candidate_id=%s accepted=%s category=%s",
                    task.task_id,
                    record.attempt_index,
                    record.candidate_id,
                    check_result.accepted,
                    check_result.category.value,
                )

                if check_result.parsed_feedback is not None:
                    feedback_history.append(check_result.parsed_feedback)

                if check_result.accepted:
                    logger.info(
                        "Controller accepted proof: task_id=%s attempt_index=%d",
                        task.task_id,
                        record.attempt_index,
                    )
                    return ControllerResult(
                        task=task,
                        accepted=True,
                        attempts=tuple(attempts),
                        budget=self.budget.snapshot(),
                        stop_reason="accepted",
                        accepted_attempt=record,
                    )

                if (
                    self.config.stop_on_tool_unavailable
                    and check_result.category == DiagnosticCategory.TOOL_UNAVAILABLE
                ):
                    stop_reason = "tool_unavailable"
                    logger.warning("Controller stopped: task_id=%s reason=%s", task.task_id, stop_reason)
                    return ControllerResult(
                        task=task,
                        accepted=False,
                        attempts=tuple(attempts),
                        budget=self.budget.snapshot(),
                        stop_reason=stop_reason,
                    )

        reason = self.budget.exhausted_reason()
        if reason is not None:
            stop_reason = f"budget:{reason}"
        logger.info(
            "Controller run finished: task_id=%s accepted=False stop_reason=%s attempts=%d",
            task.task_id,
            stop_reason,
            len(attempts),
        )

        return ControllerResult(
            task=task,
            accepted=False,
            attempts=tuple(attempts),
            budget=self.budget.snapshot(),
            stop_reason=stop_reason,
        )
