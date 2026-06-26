"""Run state and result construction for the structured controller.

Parallel to ``agent/search/controller/results.py`` but built on the structured
``_StructuredRunState``: the structured loop's authoritative state lives in the
:class:`ProofWorkspace`, so the run state only accumulates the shared
attempt/metric/safety observations that flow into :class:`RunMetrics` and the
trace. We reuse :func:`summarize_run` / :func:`new_sample_id` from the common
metrics module so structured and minimal runs produce identical observation
fields for cross-mode comparison.

We deliberately do not refactor ``results.py``: its builders are coupled to the
minimal ``_ControllerRunState`` (linear feedback history, self-managed memory),
and sharing them would risk the minimal path. Keeping a parallel builder here
is the lower-risk choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent.proof_system.workspace import ObligationStatus, WorkspaceStatus
from ..budget import BudgetManager
from ..controller.types import AttemptRecord, ControllerResult
from ..execution import ExecutionMode
from ..metrics import new_sample_id, summarize_run
from .summary import build_result_summary

if TYPE_CHECKING:
    from agent.proof_system.assembler import AssemblyResult
    from agent.proof_system.base import ProofTask
    from agent.proof_system.workspace import ProofWorkspace
    from ..safety import SafetyReviewer


@dataclass
class _StructuredRunState:
    """Mutable accumulator for one structured run's shared observations.

    The authoritative search state is the :class:`ProofWorkspace`; this object
    only holds the attempt stream and safety rejections that the Phase 0
    metrics layer and the trace need regardless of execution mode.
    """

    attempts: list[AttemptRecord] = field(default_factory=list)
    attempt_metrics: list = field(default_factory=list)
    attempt_index: int = 0
    stop_reason: str = "budget"
    sample_id: str = field(default_factory=new_sample_id)
    safety_rejections: list[dict[str, Any]] = field(default_factory=list)
    feedback_history: list = field(default_factory=list)
    current_retrieved: tuple = ()
    retrieved_history: list = field(default_factory=list)
    retrieved_this_iteration: bool = False
    # Phase 7.2: proposals the generator emitted but the controller did not
    # execute (the kinds whose executors have not landed). The legacy adapter
    # only emits IMPLEMENT/REPAIR, so this stays empty on the baseline path; it
    # records what a native structured generator tried that the loop skipped.
    skipped_proposals: list[dict[str, Any]] = field(default_factory=list)
    # Phase 7.4: DECOMPOSE proposals the controller *executed* (structural
    # obligation splits). Unlike attempts these consume no check and no model
    # call, so they are not AttemptRecords; they are recorded for the trace so
    # the search tree's decompositions are visible. Empty on the baseline path
    # (only a native generator emits DECOMPOSE).
    decompose_records: list[dict[str, Any]] = field(default_factory=list)
    # Phase 7.6: argument-layer edits (PROPOSE_ARGUMENT / REFINE_ARGUMENT) the
    # controller *executed*. Like decompose these are structural (no check, no
    # model call of their own), recorded for trace visibility.
    argument_records: list[dict[str, Any]] = field(default_factory=list)
    # Phase 7.6: CHANGE_REPRESENTATION forks the controller *executed* — one
    # record per representation branch spawned.
    representation_records: list[dict[str, Any]] = field(default_factory=list)


#: Obligation statuses that still represent work the search could pursue. A
#: run whose every active obligation is outside this set has no live route
#: left — only verified (ACCEPTED) or dead (BLOCKED) obligations remain.
_SOLVABLE_STATUSES: frozenset[ObligationStatus] = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)


def finalize_workspace_status(
    workspace: ProofWorkspace, *, accepted: bool
) -> WorkspaceStatus:
    """Derive the deterministic terminal status of a structured run.

    This is the Phase 7.7 run finalizer. ``accepted`` runs are ``ACCEPTED``
    (the assembler already set this on its success path; the finalizer is
    idempotent). For a run that did not close:

    * if no active obligation is still solvable (every route is ACCEPTED or
      BLOCKED) and the root is not accepted, the run is **BLOCKED** — all
      remaining lines are mechanical dead-ends;
    * otherwise, if at least one *non-root* active obligation is ACCEPTED,
      the run is **PARTIAL** — it produced a reusable verified sub-result even
      though the root did not close (``tmp/plan1.md`` §budget: insufficient
      budget can only yield PARTIAL or BLOCKED);
    * otherwise the run stays **SEARCHING** — a single-root run (or one with
      no verified helpers) that ran out of budget mid-search is neither a
      partial success nor a clean failure, so it is not mislabelled.

    The single-root baseline never has a non-root ACCEPTED obligation, so a
    budget-exhausted baseline run stays SEARCHING rather than being misreported
    as PARTIAL.
    """
    if accepted:
        return WorkspaceStatus.ACCEPTED

    graph = workspace.obligation_graph
    active = graph.active()
    root = graph.root()
    root_accepted = root is not None and root.status == ObligationStatus.ACCEPTED

    has_solvable = any(
        obligation.status in _SOLVABLE_STATUSES for obligation in active
    )
    if not has_solvable and not root_accepted:
        return WorkspaceStatus.BLOCKED

    has_verified_helper = any(
        obligation.obligation_id not in workspace.root_obligation_ids
        and obligation.status == ObligationStatus.ACCEPTED
        for obligation in active
    )
    if has_verified_helper:
        return WorkspaceStatus.PARTIAL

    return WorkspaceStatus.SEARCHING


def build_structured_result(
    state: _StructuredRunState,
    task: ProofTask,
    workspace: ProofWorkspace,
    *,
    accepted: bool,
    stop_reason: str,
    execution_mode: ExecutionMode,
    budget: BudgetManager,
    safety_reviewer: SafetyReviewer,
    assembly_outcome: AssemblyResult | None = None,
) -> ControllerResult:
    """Construct the :class:`ControllerResult` for one structured run.

    Mirrors ``build_final_result`` / ``build_accepted_result``: same fields,
    same metrics roll-up, plus the serialized workspace under
    ``metadata["workspace"]`` so :func:`trace_store.workspace_payload` surfaces
    it in the run summary.

    ``assembly_outcome`` carries the final-assembly result when the run reached
    :meth:`StructuredController._assemble_and_finalize`. It is surfaced two
    ways: the raw ``AssemblyResult.to_dict`` (with its ``errors``) under
    ``metadata["assembly"]`` — which previously was dropped on assembly failure
    — and the derived machine-assertable view under
    ``metadata["result_summary"]``. When the run never reached assembly
    (budget exhaustion, ``no_actions``, ``tool_unavailable``), only the
    ``result_summary`` is written and ``assembly.executed`` is ``False``.
    """
    snapshot = budget.snapshot()
    metrics = summarize_run(
        sample_id=state.sample_id,
        task_id=task.task_id,
        accepted=accepted,
        stop_reason=stop_reason,
        attempts=state.attempt_metrics,
        budget_checks_used=snapshot.checks_used,
        budget_model_calls_used=snapshot.model_calls_used,
        budget_exhausted_reason=snapshot.exhausted_reason,
        execution_mode=execution_mode,
    )
    accepted_attempt = (
        state.attempts[-1] if accepted and state.attempts else None
    )
    # Phase 7.7: derive the terminal workspace status from the obligation DAG
    # so the serialized workspace and the result summary report a honest
    # ACCEPTED / PARTIAL / BLOCKED instead of the in-progress SEARCHING the run
    # carried while looping. Idempotent on the accepted path (already ACCEPTED).
    workspace = workspace.successor(
        status=finalize_workspace_status(workspace, accepted=accepted)
    )
    metadata: dict[str, Any] = {
        "workspace": workspace.to_dict(),
        "safety_rejections": tuple(state.safety_rejections),
        "safety_reviewer": type(safety_reviewer).__name__,
        "skipped_proposals": tuple(state.skipped_proposals),
        "decompose_records": tuple(state.decompose_records),
        "argument_records": tuple(state.argument_records),
        "representation_records": tuple(state.representation_records),
        "result_summary": build_result_summary(
            workspace, assembly_result=assembly_outcome
        ).to_dict(),
    }
    if assembly_outcome is not None:
        metadata["assembly"] = assembly_outcome.to_dict()
    return ControllerResult(
        task=task,
        accepted=accepted,
        attempts=tuple(state.attempts),
        budget=snapshot,
        stop_reason=stop_reason,
        accepted_attempt=accepted_attempt,
        metrics=metrics,
        metadata=metadata,
    )
