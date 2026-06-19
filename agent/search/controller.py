"""Budget-aware controller loop for running proof attempts end to end."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from .action import ActionCandidate, ActionGenerationRequest, ActionGenerator
from .budget import BudgetConfig, BudgetManager, BudgetSnapshot
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
        ...


@dataclass(frozen=True)
class ControllerConfig:
    """Small policy knobs for the MVP controller."""

    max_candidates_per_model_call: int = 1
    candidate_extension: str = ".lean"
    stop_on_tool_unavailable: bool = True
    max_repair_rounds: int = 2
    max_feedback_history: int = 5
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


@dataclass
class _ControllerRunState:
    """Mutable working state for a single controller run.

    Encapsulates everything that changes from one loop iteration to the next so
    that ``run()`` can be expressed as a short pipeline of phase methods.
    """

    attempts: list[AttemptRecord] = field(default_factory=list)
    feedback_history: list[ParsedFeedback] = field(default_factory=list)
    retrieved_history: list[RetrievalResult] = field(default_factory=list)
    seen_candidate_keys: set[tuple[str, str]] = field(default_factory=set)
    current_retrieved: tuple[RetrievalResult, ...] = ()
    stop_reason: str = "budget"
    attempt_index: int = 0
    repair_rounds: int = 0
    retrieved_this_episode: bool = False
    next_meta_action: str = "expand"


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
        self.repair_generator = repair_generator or action_generator
        self.retriever = retriever
        self.budget = BudgetManager(budget_config)
        self.config = config or ControllerConfig()

    def run(self, task: ProofTask) -> ControllerResult:
        logger.info("Controller run started: task_id=%s", task.task_id)
        state = self._initial_state()

        while self.budget.can_check():
            # Mark the start of a "retrieve + expand" cycle. The flag is reset
            # below whenever we enter a plain expand iteration, so each expand
            # cycle is allowed to retrieve at most once.
            if state.next_meta_action == "retrieve":
                state.retrieved_this_episode = True

            state.current_retrieved = self._maybe_retrieve(
                task,
                state.feedback_history[-1] if state.feedback_history else None,
                state.next_meta_action,
            )
            if state.current_retrieved:
                state.retrieved_history.extend(state.current_retrieved)

            is_repair_iteration = state.next_meta_action == "repair"
            if is_repair_iteration:
                generator = self.repair_generator
                max_candidates = self.config.max_candidates_per_model_call
                if generator is self.action_generator:
                    if not self.budget.can_call_model():
                        state.stop_reason = "budget:model_calls"
                        logger.info(
                            "Controller stopped before repair model call: task_id=%s reason=%s",
                            task.task_id,
                            state.stop_reason,
                        )
                        break
                    self.budget.reserve_model_call()
            else:
                if not self.budget.can_call_model():
                    state.stop_reason = "budget:model_calls"
                    logger.info(
                        "Controller stopped before model call: task_id=%s reason=%s",
                        task.task_id,
                        state.stop_reason,
                    )
                    break
                self.budget.reserve_model_call()
                generator = self.action_generator
                max_candidates = self.config.max_candidates_per_model_call
                state.repair_rounds = 0
                if state.next_meta_action != "retrieve":
                    # A fresh expand cycle starts here; allow another retrieve.
                    state.retrieved_this_episode = False

            actions = self._generate_actions(
                state, task, generator, max_candidates
            )
            if not actions:
                if is_repair_iteration:
                    state.next_meta_action = "expand"
                    continue
                state.stop_reason = "no_actions"
                logger.info(
                    "Controller stopped: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
                break

            accepted_record = self._evaluate_candidates(
                state, task, actions, max_candidates, is_repair_iteration
            )
            if accepted_record is not None:
                return self._build_accepted_result(state, task, accepted_record)
            if state.stop_reason == "tool_unavailable":
                return self._build_tool_unavailable_result(state, task)
            if state.stop_reason == "no_new_actions":
                break

        return self._build_final_result(state, task)

    def _initial_state(self) -> _ControllerRunState:
        return _ControllerRunState(
            next_meta_action=(
                "retrieve"
                if self.config.retrieve_before_first_model_call
                else "expand"
            ),
        )

    def _generate_actions(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        generator: ActionGenerator,
        max_candidates: int,
    ) -> tuple[ActionCandidate, ...]:
        request = self._build_generation_request(
            state, task, generator, max_candidates
        )
        actions = tuple(generator.generate(request))
        logger.info(
            "Proof generation completed: task_id=%s attempt_index=%d meta_action=%s candidates=%d",
            task.task_id,
            state.attempt_index,
            state.next_meta_action,
            len(actions),
        )
        return actions

    def _build_generation_request(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        generator: ActionGenerator,
        max_candidates: int,
    ) -> ActionGenerationRequest:
        budget_snapshot = self.budget.snapshot()
        encoded_state = encode_proof_state(
            task,
            feedback_history=state.feedback_history,
            budget=budget_snapshot,
            metadata={"next_meta_action": state.next_meta_action},
        )
        logger.info(
            "Proof generation started: task_id=%s attempt_index=%d meta_action=%s previous_feedback=%d",
            task.task_id,
            state.attempt_index,
            state.next_meta_action,
            len(state.feedback_history),
        )
        previous_attempt = None
        if state.attempts:
            last = state.attempts[-1]
            previous_attempt = {
                "attempt_index": last.attempt_index,
                "proof_text": last.edit.text,
                "category": last.check_result.category.value,
                "raw_output": last.check_result.raw_output,
            }
        return ActionGenerationRequest(
            task=task,
            attempt_index=state.attempt_index,
            previous_feedback=tuple(state.feedback_history),
            max_candidates=max_candidates,
            metadata={
                "meta_action": state.next_meta_action,
                "encoded_state": encoded_state,
                "retrieved_results": state.current_retrieved,
                "retrieved_history": tuple(state.retrieved_history),
                "previous_attempt": previous_attempt,
                "budget": budget_snapshot,
            },
        )

    def _evaluate_candidates(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        actions: tuple[ActionCandidate, ...],
        max_candidates: int,
        is_repair_iteration: bool,
    ) -> AttemptRecord | None:
        checked_any = False
        for action in actions[:max_candidates]:
            if not self.budget.can_check():
                state.stop_reason = "budget"
                logger.info(
                    "Controller stopped before check: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
                break

            candidate_key = (action.action, action.proof_text.strip())
            if candidate_key in state.seen_candidate_keys:
                logger.debug(
                    "Skipping duplicate candidate: task_id=%s attempt_index=%d action=%s",
                    task.task_id,
                    state.attempt_index,
                    action.action,
                )
                continue
            state.seen_candidate_keys.add(candidate_key)

            record = self._check_single_candidate(state, task, action)
            state.attempts.append(record)
            state.attempt_index += 1
            logger.info(
                "Candidate checked: task_id=%s attempt_index=%d candidate_id=%s accepted=%s category=%s",
                task.task_id,
                record.attempt_index,
                record.candidate_id,
                record.check_result.accepted,
                record.check_result.category.value,
            )

            if record.check_result.parsed_feedback is not None:
                state.feedback_history.append(record.check_result.parsed_feedback)
                if len(state.feedback_history) > self.config.max_feedback_history:
                    state.feedback_history[:] = state.feedback_history[
                        -self.config.max_feedback_history :
                    ]
            checked_any = True

            if record.check_result.accepted:
                logger.info(
                    "Controller accepted proof: task_id=%s attempt_index=%d",
                    task.task_id,
                    record.attempt_index,
                )
                return record

            if (
                self.config.stop_on_tool_unavailable
                and record.check_result.category == DiagnosticCategory.TOOL_UNAVAILABLE
            ):
                state.stop_reason = "tool_unavailable"
                logger.warning(
                    "Controller stopped: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
                return None

            # ``state.repair_rounds`` counts completed repair iterations for
            # the current expand cycle. When we are inside a repair iteration we
            # pass repair_rounds + 1 here so the boundary check in
            # ``_choose_next_meta_action`` (``< max_repair_rounds``) matches the
            # old pre-execution gate. Keep this aligned with the increment at
            # the end of this method.
            state.next_meta_action = self._choose_next_meta_action(
                record.check_result.category,
                state.repair_rounds + 1 if is_repair_iteration else state.repair_rounds,
                retrieved_this_episode=state.retrieved_this_episode,
            )

        if not checked_any:
            if is_repair_iteration:
                state.next_meta_action = "expand"
                return None
            state.stop_reason = "no_new_actions"
            logger.info(
                "Controller stopped: task_id=%s reason=%s",
                task.task_id,
                state.stop_reason,
            )
            return None

        # Record that a repair iteration has completed. Must stay aligned with
        # the +1 passed to ``_choose_next_meta_action`` above.
        if is_repair_iteration:
            state.repair_rounds += 1
        return None

    def _check_single_candidate(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        action: ActionCandidate,
    ) -> AttemptRecord:
        edit = _edit_with_controller_metadata(
            action.to_edit(),
            meta_action=state.next_meta_action,
            retrieved=state.current_retrieved,
        )
        logger.debug(
            "Rendering candidate: task_id=%s attempt_index=%d action=%s",
            task.task_id,
            state.attempt_index,
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
        logger.info(
            "Candidate check started: task_id=%s attempt_index=%d candidate_id=%s timeout=%s",
            task.task_id,
            state.attempt_index,
            materialized.candidate_id,
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
        return AttemptRecord(
            attempt_index=state.attempt_index,
            candidate_id=materialized.candidate_id,
            edit=edit,
            candidate_file=materialized.path,
            check_result=check_result,
        )

    def _build_accepted_result(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        record: AttemptRecord,
    ) -> ControllerResult:
        logger.info(
            "Controller accepted proof: task_id=%s attempt_index=%d",
            task.task_id,
            record.attempt_index,
        )
        return ControllerResult(
            task=task,
            accepted=True,
            attempts=tuple(state.attempts),
            budget=self.budget.snapshot(),
            stop_reason="accepted",
            accepted_attempt=record,
            metadata={
                "retrieved_results": tuple(state.retrieved_history),
                "feedback_count": len(state.feedback_history),
            },
        )

    def _build_tool_unavailable_result(
        self,
        state: _ControllerRunState,
        task: ProofTask,
    ) -> ControllerResult:
        logger.warning(
            "Controller stopped: task_id=%s reason=%s",
            task.task_id,
            state.stop_reason,
        )
        return ControllerResult(
            task=task,
            accepted=False,
            attempts=tuple(state.attempts),
            budget=self.budget.snapshot(),
            stop_reason=state.stop_reason,
            metadata={
                "retrieved_results": tuple(state.retrieved_history),
                "feedback_count": len(state.feedback_history),
            },
        )

    def _build_final_result(
        self,
        state: _ControllerRunState,
        task: ProofTask,
    ) -> ControllerResult:
        reason = self.budget.exhausted_reason()
        if reason is not None:
            state.stop_reason = f"budget:{reason}"
        logger.info(
            "Controller run finished: task_id=%s accepted=False stop_reason=%s attempts=%d",
            task.task_id,
            state.stop_reason,
            len(state.attempts),
        )
        return ControllerResult(
            task=task,
            accepted=False,
            attempts=tuple(state.attempts),
            budget=self.budget.snapshot(),
            stop_reason=state.stop_reason,
            metadata={
                "retrieved_results": tuple(state.retrieved_history),
                "feedback_count": len(state.feedback_history),
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
        *,
        retrieved_this_episode: bool,
    ) -> str:
        if (
            category in self.config.retrieve_on_categories
            and self.retriever is not None
            and not retrieved_this_episode
        ):
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
