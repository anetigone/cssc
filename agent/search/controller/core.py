"""Budget-aware controller loop for running proof attempts end to end."""

from __future__ import annotations

import logging
from dataclasses import replace

from ..action import (
    ActionCandidate,
    ActionGenerationError,
    ActionGenerationRequest,
    ActionGenerator,
)
from ..budget import BudgetConfig, BudgetManager
from .context import maybe_retrieve, summarize_context
from ..memory import MemoryProcessor, MemoryUpdate
from ..metrics import attempt_metric
from ..runtime_ledger import record_checker_event, record_generation_events
from .results import (
    build_accepted_result,
    build_final_result,
    build_tool_unavailable_result,
)
from ..safety import SafetyReviewer, SafetyVerdict, StatementSafetyReviewer
from ..state_encoder import encode_proof_state
from .types import (
    AttemptRecord,
    ControllerConfig,
    ControllerResult,
    Retriever,
    _ControllerRunState,
)
from .utils import _edit_with_controller_metadata, _proof_phase
from ...agents.context import ContextSummarizer
from ...proof_system.base import (
    DiagnosticCategory,
    ProofSystemAdapter,
    ProofTask,
)
from ...runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace


logger = logging.getLogger(__name__)


def _record_model_usage(
    records: list[dict[str, int]],
    actions: tuple[ActionCandidate, ...],
) -> None:
    if not actions:
        return
    usage = actions[0].metadata.get("token_usage")
    if isinstance(usage, dict):
        records.append(
            {
                key: value
                for key, value in usage.items()
                if isinstance(key, str) and isinstance(value, int)
            }
        )


class ProofController:
    """Coordinate action generation, rendering, materialization, and checking.

    Single-proof loop: each model call proposes one (or a few) candidates,
    the checker vets them, and the most recent failure feedback is fed back
    into the next model call. Every model call and every check counts against
    the budget. There is no separate repair agent.
    """
    def __init__(
        self,
        *,
        adapter: ProofSystemAdapter,
        action_generator: ActionGenerator,
        workspace: AttemptWorkspace,
        check_workspace: EphemeralCheckWorkspace | None = None,
        retriever: Retriever | None = None,
        context_summarizer: ContextSummarizer | None = None,
        budget_config: BudgetConfig | None = None,
        config: ControllerConfig | None = None,
        safety_reviewer: SafetyReviewer | None = None,
    ) -> None:
        self.adapter = adapter
        self.action_generator = action_generator
        self.workspace = workspace
        self.check_workspace = check_workspace
        self.retriever = retriever
        self.context_summarizer = context_summarizer
        self.budget = BudgetManager(budget_config)
        self.config = config or ControllerConfig()
        self.memory_processor = MemoryProcessor()
        self.safety_reviewer = safety_reviewer or StatementSafetyReviewer()

    def run(self, task: ProofTask) -> ControllerResult:
        logger.info("Controller run started: task_id=%s", task.task_id)
        state = self._initial_state()

        while self.budget.can_check():
            # A fresh iteration starts here; allow this iteration to retrieve.
            state.retrieved_this_iteration = False
            state.current_retrieved = maybe_retrieve(
                task,
                state,
                self.retriever,
                self.config,
                is_first_iteration=not state.attempts,
            )
            if state.current_retrieved:
                state.retrieved_history.extend(state.current_retrieved)

            if not self.budget.can_call_model():
                state.stop_reason = "budget:model_calls"
                logger.info(
                    "Controller stopped before model call: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
                break
            self.budget.reserve_model_call()

            max_candidates = self.config.max_candidates_per_model_call
            try:
                actions = self._generate_actions(state, task, max_candidates)
            except ActionGenerationError as exc:
                failure = {
                    "attempt_index": state.attempt_index,
                    "reason": exc.reason,
                    "message": str(exc),
                    **exc.metadata,
                }
                state.generation_failures.append(failure)
                cost_metadata = dict(exc.metadata)
                if "pricing" in task.metadata:
                    cost_metadata.setdefault("pricing", task.metadata["pricing"])
                state.cost_ledger = record_generation_events(
                    state.cost_ledger,
                    metadata=cost_metadata,
                    attempt_index=state.attempt_index,
                    fallback_request_id=f"proposal:{state.sample_id}:{self.budget.model_calls_used}",
                    status="failed",
                )
                usage = exc.metadata.get("token_usage")
                if isinstance(usage, dict):
                    state.model_usage.append(dict(usage))
                state.stop_reason = f"generation:{exc.reason}"
                logger.warning(
                    "Controller generation failed: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
                break
            _record_model_usage(state.model_usage, actions)
            if actions:
                cost_metadata = dict(actions[0].metadata)
                if "pricing" in task.metadata:
                    cost_metadata.setdefault("pricing", task.metadata["pricing"])
                state.cost_ledger = record_generation_events(
                    state.cost_ledger,
                    metadata=cost_metadata,
                    attempt_index=state.attempt_index,
                    fallback_request_id=f"proposal:{state.sample_id}:{self.budget.model_calls_used}",
                    status="completed",
                )
            if not actions:
                state.stop_reason = "no_actions"
                logger.info(
                    "Controller stopped: task_id=%s reason=%s",
                    task.task_id,
                    state.stop_reason,
                )
                break

            accepted_record = self._evaluate_candidates(
                state, task, actions, max_candidates
            )
            if accepted_record is not None:
                return build_accepted_result(
                    state,
                    task,
                    accepted_record,
                    self.budget,
                    self.config.execution_mode,
                    self.safety_reviewer,
                )
            if state.stop_reason == "tool_unavailable":
                return build_tool_unavailable_result(
                    state,
                    task,
                    self.budget,
                    self.config.execution_mode,
                    self.safety_reviewer,
                )
            if state.stop_reason == "no_new_actions":
                break

        return build_final_result(
            state,
            task,
            self.budget,
            self.config.execution_mode,
            self.safety_reviewer,
        )

    def _initial_state(self) -> _ControllerRunState:
        return _ControllerRunState()

    def _generate_actions(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        max_candidates: int,
    ) -> tuple[ActionCandidate, ...]:
        request = self._build_generation_request(state, task, max_candidates)
        actions = tuple(self.action_generator.generate(request))
        logger.info(
            "Proof generation completed: task_id=%s attempt_index=%d candidates=%d",
            task.task_id,
            state.attempt_index,
            len(actions),
        )
        return actions
    def _build_generation_request(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        max_candidates: int,
    ) -> ActionGenerationRequest:
        budget_snapshot = self.budget.snapshot()
        encoded_state = encode_proof_state(
            task,
            feedback_history=state.feedback_history,
            budget=budget_snapshot,
        )
        proof_phase = _proof_phase(state)
        logger.info(
            "Proof generation started: task_id=%s attempt_index=%d phase=%s previous_feedback=%d",
            task.task_id,
            state.attempt_index,
            proof_phase,
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
        summarized_context = summarize_context(
            task,
            state,
            self.context_summarizer,
            previous_attempt,
        )
        return ActionGenerationRequest(
            task=task,
            attempt_index=state.attempt_index,
            previous_feedback=tuple(state.feedback_history),
            max_candidates=max_candidates,
            metadata={
                "proof_phase": proof_phase,
                "encoded_state": encoded_state,
                "retrieved_results": state.current_retrieved,
                "retrieved_history": tuple(state.retrieved_history),
                "previous_attempt": previous_attempt,
                "summarized_context": summarized_context,
                "proof_memory": state.memory,
                "budget": budget_snapshot,
            },
        )

    def _evaluate_candidates(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        actions: tuple[ActionCandidate, ...],
        max_candidates: int,
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
            # Resolve the effective outcome before folding into memory: an
            # accepted candidate still counts as unsolved if the safety review
            # catches a shortcut, and the memory must not promote it.
            safety_verdict = self._review_accepted_candidate(task, record, state)
            effective_accepted = safety_verdict.accepted
            self._update_memory(state, task, record, safety_verdict)
            metric = attempt_metric(
                record.attempt_index,
                action=record.edit.action,
                check_result=record.check_result,
            )
            state.attempt_metrics.append(metric)
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

            if effective_accepted:
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

        if not checked_any:
            state.stop_reason = "no_new_actions"
            logger.info(
                "Controller stopped: task_id=%s reason=%s",
                task.task_id,
                state.stop_reason,
            )
            return None
        return None

    def _check_single_candidate(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        action: ActionCandidate,
    ) -> AttemptRecord:
        edit = _edit_with_controller_metadata(
            action.to_edit(),
            proof_phase=_proof_phase(state),
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
        state.cost_ledger = record_checker_event(
            state.cost_ledger,
            attempt_index=state.attempt_index,
            check_result=check_result,
        )
        return AttemptRecord(
            attempt_index=state.attempt_index,
            candidate_id=materialized.candidate_id,
            edit=edit,
            candidate_file=materialized.path,
            check_result=check_result,
        )

    def _review_accepted_candidate(
        self,
        task: ProofTask,
        record: AttemptRecord,
        state: _ControllerRunState,
    ) -> SafetyVerdict:
        """Return the effective verdict and retain rejected safety evidence."""
        if not record.check_result.accepted:
            return SafetyVerdict(accepted=False)

        candidate_source = self.adapter.render_candidate(task, record.edit)
        verdict = self.safety_reviewer.accepts(
            task, candidate_source, record.check_result
        )
        if not verdict.accepted:
            state.safety_rejections.append(
                {
                    "attempt_index": record.attempt_index,
                    "candidate_id": record.candidate_id,
                    "reasons": verdict.reasons,
                    "metadata": dict(verdict.metadata),
                }
            )
        return verdict

    def _update_memory(
        self,
        state: _ControllerRunState,
        task: ProofTask,
        record: AttemptRecord,
        safety_verdict: SafetyVerdict,
    ) -> None:
        """Fold one checked candidate's outcome into the self-managed memory."""
        state.memory = self.memory_processor.update(
            state.memory,
            MemoryUpdate(
                task=task,
                attempt_index=record.attempt_index,
                proof_text=record.edit.text,
                action=record.edit.action,
                check_result=record.check_result,
                feedback=record.check_result.parsed_feedback,
                effective_accepted=safety_verdict.accepted,
                safety_reasons=safety_verdict.reasons,
            ),
        )
