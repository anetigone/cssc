"""Budget-aware controller loop for running proof attempts end to end."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from .action import ActionGenerationRequest, ActionGenerator
from .budget import BudgetConfig, BudgetManager, BudgetSnapshot
from .repair import FeedbackRepairGenerator
from .state_encoder import encode_proof_state
from ..proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProofSystemAdapter,
    ProofTask,
)
from ..retrieval import RetrievalResult
from ..runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace


logger = logging.getLogger(__name__)


class Retriever(Protocol):
    """Minimal retrieval boundary used by the controller."""

    def retrieve(
        self,
        query: str | None = None,
        *,
        task: ProofTask | None = None,
        feedback: ParsedFeedback | None = None,
        top_k: int = 5,
    ) -> tuple[RetrievalResult, ...]:
        """Return snippets relevant to the current proof state."""


@dataclass(frozen=True)
class ControllerConfig:
    """Small policy knobs for the MVP controller."""

    max_candidates_per_model_call: int = 1
    candidate_extension: str = ".lean"
    stop_on_tool_unavailable: bool = True
    max_repair_rounds: int = 2
    max_retrieval_results: int = 5
    retrieve_before_first_model_call: bool = False
    retrieve_on_categories: tuple[DiagnosticCategory, ...] = (
        DiagnosticCategory.UNKNOWN_IDENTIFIER,
        DiagnosticCategory.INVALID_REFERENCE,
        DiagnosticCategory.UNSOLVED_GOALS,
    )


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
        check_workspace: EphemeralCheckWorkspace | None = None,
        repair_generator: ActionGenerator | None = None,
        retriever: Retriever | None = None,
        budget_config: BudgetConfig | None = None,
        config: ControllerConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.action_generator = action_generator
        self.workspace = workspace
        self.check_workspace = check_workspace
        self.repair_generator = repair_generator or FeedbackRepairGenerator()
        self.retriever = retriever
        self.budget = BudgetManager(budget_config)
        self.config = config or ControllerConfig()

    def run(self, task: ProofTask) -> ControllerResult:
        logger.info("Controller run started: task_id=%s", task.task_id)
        attempts: list[AttemptRecord] = []
        feedback_history: list[ParsedFeedback] = []
        retrieved_history: list[RetrievalResult] = []
        seen_candidate_keys: set[tuple[str, str]] = set()
        stop_reason = "budget"
        attempt_index = 0
        repair_rounds = 0
        next_meta_action = "retrieve" if self.config.retrieve_before_first_model_call else "expand"

        while self.budget.can_check():
            feedback = feedback_history[-1] if feedback_history else None
            retrieved = self._maybe_retrieve(task, feedback, next_meta_action)
            if retrieved:
                retrieved_history.extend(retrieved)

            if next_meta_action == "repair":
                if repair_rounds >= self.config.max_repair_rounds:
                    next_meta_action = "expand"
                    continue
                generator = self.repair_generator
                max_candidates = self.config.max_candidates_per_model_call
                repair_rounds += 1
            else:
                if not self.budget.can_call_model():
                    stop_reason = "budget:model_calls"
                    logger.info("Controller stopped before model call: task_id=%s reason=%s", task.task_id, stop_reason)
                    break
                self.budget.reserve_model_call()
                generator = self.action_generator
                max_candidates = self.config.max_candidates_per_model_call
                repair_rounds = 0

            budget_snapshot = self.budget.snapshot()
            encoded_state = encode_proof_state(
                task,
                feedback_history=feedback_history,
                budget=budget_snapshot,
                metadata={"next_meta_action": next_meta_action},
            )
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
                max_candidates=max_candidates,
                metadata={
                    "meta_action": next_meta_action,
                    "encoded_state": encoded_state,
                    "retrieved_results": tuple(retrieved),
                    "retrieved_history": tuple(retrieved_history),
                    "budget": budget_snapshot,
                },
            )
            actions = tuple(generator.generate(request))
            logger.debug(
                "Generated %d action(s): task_id=%s attempt_index=%d",
                len(actions),
                task.task_id,
                attempt_index,
            )
            if not actions:
                if next_meta_action == "repair":
                    next_meta_action = "expand"
                    continue
                stop_reason = "no_actions"
                logger.info("Controller stopped: task_id=%s reason=%s", task.task_id, stop_reason)
                break

            checked_any = False
            for action in actions[:max_candidates]:
                if not self.budget.can_check():
                    stop_reason = "budget"
                    logger.info("Controller stopped before check: task_id=%s reason=%s", task.task_id, stop_reason)
                    break

                candidate_key = (action.action, action.proof_text.strip())
                if candidate_key in seen_candidate_keys:
                    logger.debug(
                        "Skipping duplicate candidate: task_id=%s attempt_index=%d action=%s",
                        task.task_id,
                        attempt_index,
                        action.action,
                    )
                    continue
                seen_candidate_keys.add(candidate_key)

                edit = _edit_with_controller_metadata(
                    action.to_edit(),
                    meta_action=next_meta_action,
                    retrieved=tuple(retrieved),
                )
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
                if self.check_workspace is None:
                    check_result = self.adapter.check(materialized.path, budget_slice)
                else:
                    with self.check_workspace.materialize_candidate(
                        task,
                        candidate_id=materialized.candidate_id,
                        source=source,
                        extension=self.config.candidate_extension,
                    ) as check_candidate:
                        check_result = self.adapter.check(check_candidate.path, budget_slice)
                    check_result = replace(check_result, candidate_file=materialized.path)
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
                checked_any = True

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
                        metadata={
                            "retrieved_results": tuple(retrieved_history),
                            "feedback_count": len(feedback_history),
                        },
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
                        metadata={
                            "retrieved_results": tuple(retrieved_history),
                            "feedback_count": len(feedback_history),
                        },
                    )

                next_meta_action = self._choose_next_meta_action(check_result.category, repair_rounds)

            if not checked_any:
                if next_meta_action == "repair":
                    next_meta_action = "expand"
                    continue
                stop_reason = "no_new_actions"
                logger.info("Controller stopped: task_id=%s reason=%s", task.task_id, stop_reason)
                break

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
            metadata={
                "retrieved_results": tuple(retrieved_history),
                "feedback_count": len(feedback_history),
            },
        )

    def _maybe_retrieve(
        self,
        task: ProofTask,
        feedback: ParsedFeedback | None,
        meta_action: str,
    ) -> tuple[RetrievalResult, ...]:
        if self.retriever is None:
            return ()
        if meta_action not in {"retrieve", "expand"}:
            return ()
        if meta_action == "expand" and feedback is not None:
            return ()
        logger.debug(
            "Retrieving context: task_id=%s meta_action=%s feedback_category=%s",
            task.task_id,
            meta_action,
            feedback.category.value if feedback else None,
        )
        return self.retriever.retrieve(
            task=task,
            feedback=feedback,
            top_k=self.config.max_retrieval_results,
        )

    def _choose_next_meta_action(
        self,
        category: DiagnosticCategory,
        repair_rounds: int,
    ) -> str:
        if category in self.config.retrieve_on_categories and self.retriever is not None:
            return "retrieve"
        if repair_rounds < self.config.max_repair_rounds and category in _REPAIRABLE_CATEGORIES:
            return "repair"
        return "expand"


_REPAIRABLE_CATEGORIES = {
    DiagnosticCategory.PARSER_ERROR,
    DiagnosticCategory.TYPE_MISMATCH,
    DiagnosticCategory.UNSOLVED_GOALS,
    DiagnosticCategory.TACTIC_FAILED,
    DiagnosticCategory.TIMEOUT,
    DiagnosticCategory.UNKNOWN,
}


def _edit_with_controller_metadata(
    edit: CandidateEdit,
    *,
    meta_action: str,
    retrieved: tuple[RetrievalResult, ...],
) -> CandidateEdit:
    metadata = dict(edit.metadata)
    metadata["meta_action"] = meta_action
    if retrieved:
        metadata["retrieved_results"] = tuple(_retrieval_payload(item) for item in retrieved)
    return CandidateEdit(
        text=edit.text,
        action=edit.action,
        parent_node_id=edit.parent_node_id,
        metadata=metadata,
    )


def _retrieval_payload(result: RetrievalResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "source_path": result.source_path,
        "start_line": result.start_line,
        "snippet": result.snippet,
        "score": result.score,
        "metadata": result.metadata,
    }
