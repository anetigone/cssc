"""Structured branch/obligation cost attribution (Phase 8.1).

A pure *projection* of the trace data a structured run already records --- the
checked attempts, the per-iteration model-usage entries, the workspace graph,
the budget snapshot and the final assembly result --- into a four-layer cost
summary written under ``metadata["cost_summary"]`` (see
:func:`agent.search.structured.run_state.build_structured_result`).

It is observation only:

* no proof / safety / reducer / frontier / assembly semantics change;
* nothing is written back to ``RunMetrics`` / ``BudgetSnapshot`` / attempts;
* minimal mode never imports this module.

The summary keeps two non-overlapping views per branch and per obligation:

* ``direct_cost`` --- resources actually executed on that branch / obligation
  (its checks, the generator tokens of the iteration that produced proposals
  for it, and per-check wall-clock);
* ``transitive_cost`` --- ``direct_cost`` plus the direct cost of every helper
  obligation reachable along the dependency closure, so the two never
  double-count.

Cost rules (``tmp/plan1.md`` §16 / ``tmp/phase8_plan.md`` §2):

* decompose / argument / representation actions create no ``AttemptRecord`` and
  no extra model call, so they contribute ``checks=0, model_calls=0``; their
  token cost is the iteration's single generator call, tagged to the popped
  branch;
* a capability audit is ``checks=1, model_calls=0`` extra (it reuses the
  iteration's reserved call);
* final assembly is run-level cost, reported in a separate ``assembly`` layer,
  never charged to a branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent.proof_system.workspace import BranchStatus, ObligationGraph, ProofWorkspace
from agent.search.budget import BudgetSnapshot
from agent.search.controller.types import AttemptRecord
from agent.search.cost import (
    CostVector,
    add_cost,
    cost_vector_from_dict,
    cost_vector_from_metrics_and_budget,
    to_dict,
    zero_cost,
)
from agent.search.metrics import AttemptMetric, RunMetrics

if TYPE_CHECKING:
    from agent.proof_system.assembler import AssemblyResult


@dataclass(frozen=True)
class BranchCostSummary:
    """Direct + transitive cost of one search branch.

    ``transitive_cost`` adds the direct cost of helper obligations reachable
    from the branch's obligation along dependency edges, so a parent branch
    honestly reflects what its helper lemmas cost to verify.
    """

    branch_id: str
    obligation_id: str
    direct_cost: CostVector
    transitive_cost: CostVector
    attempts: int
    accepted: bool
    blocked: bool
    dormant: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "obligation_id": self.obligation_id,
            "direct_cost": to_dict(self.direct_cost),
            "transitive_cost": to_dict(self.transitive_cost),
            "attempts": self.attempts,
            "accepted": self.accepted,
            "blocked": self.blocked,
            "dormant": self.dormant,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BranchCostSummary:
        return cls(
            branch_id=data["branch_id"],
            obligation_id=data["obligation_id"],
            direct_cost=cost_vector_from_dict(data.get("direct_cost", {})),
            transitive_cost=cost_vector_from_dict(data.get("transitive_cost", {})),
            attempts=int(data.get("attempts", 0)),
            accepted=bool(data.get("accepted", False)),
            blocked=bool(data.get("blocked", False)),
            dormant=bool(data.get("dormant", False)),
        )


@dataclass(frozen=True)
class ObligationCostSummary:
    """Direct + transitive cost of one obligation across all its branches."""

    obligation_id: str
    direct_cost: CostVector
    transitive_cost: CostVector
    branch_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "direct_cost": to_dict(self.direct_cost),
            "transitive_cost": to_dict(self.transitive_cost),
            "branch_ids": list(self.branch_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObligationCostSummary:
        return cls(
            obligation_id=data["obligation_id"],
            direct_cost=cost_vector_from_dict(data.get("direct_cost", {})),
            transitive_cost=cost_vector_from_dict(data.get("transitive_cost", {})),
            branch_ids=tuple(data.get("branch_ids", ())),
        )


def _obligation_closure(obligation_id: str, graph: ObligationGraph) -> set[str]:
    """Obligation ids reachable from ``obligation_id`` along dependency edges.

    Mirrors the private walker in ``structured/reducer/core.py`` but uses only
    the public :class:`ObligationGraph` surface so this observation module does
    not couple to reducer internals. Edges run parent -> helper.
    """
    visited: set[str] = set()
    frontier = [obligation_id]
    while frontier:
        current_id = frontier.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        obligation = graph.by_id(current_id)
        if obligation is None:
            continue
        frontier.extend(obligation.dependency_ids)
    return visited


def _attempt_branch_id(attempt: AttemptRecord) -> str | None:
    """Return the branch id recorded on an attempt's edit metadata, if any."""
    raw = attempt.edit.metadata.get("structured_branch_id")
    return raw if isinstance(raw, str) else None


def _branch_direct_cost(
    branch_id: str,
    attempts: tuple[AttemptRecord, ...],
    model_usage: tuple[dict[str, Any], ...],
) -> CostVector:
    """Direct cost executed on ``branch_id``.

    ``checks`` counts that branch's checked attempts (implement / repair /
    capability each add one; structural actions add none). ``model_calls`` and
    the token fields sum the model-usage entries the controller tagged with
    this branch --- i.e. the generator iterations whose popped branch was it.
    ``elapsed_ms`` is the per-check wall-clock of those attempts, distinct from
    the run-level wall-clock.
    """
    branch_attempts = [
        attempt
        for attempt in attempts
        if _attempt_branch_id(attempt) == branch_id
    ]
    checks = len(branch_attempts)
    elapsed_ms = sum(
        round(attempt.check_result.elapsed_seconds * 1000)
        for attempt in branch_attempts
    )
    model_calls = 0
    input_tokens = 0
    output_tokens = 0
    for usage in model_usage:
        if usage.get("structured_branch_id") != branch_id:
            continue
        model_calls += 1
        input_tokens += int(usage.get("input_tokens", 0))
        output_tokens += int(usage.get("output_tokens", 0))
    return CostVector(
        model_calls=model_calls,
        checks=checks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        elapsed_ms=elapsed_ms,
    )


def build_cost_summary(
    *,
    workspace: ProofWorkspace,
    attempts: tuple[AttemptRecord, ...],
    attempt_metrics: tuple[AttemptMetric, ...],  # noqa: ARG001 - reserved for 8.4 signals
    model_usage: tuple[dict[str, Any], ...],
    run_metrics: RunMetrics | None,
    snapshot: BudgetSnapshot,
    assembly_outcome: AssemblyResult | None,
) -> dict[str, Any]:
    """Derive the four-layer ``metadata["cost_summary"]`` from trace data.

    Layers: ``branches`` (one :class:`BranchCostSummary` per workspace branch),
    ``obligations`` (one per active obligation), ``assembly`` (the run-level
    final recheck, or ``None`` if assembly was never reached) and ``run`` (the
    same run-level projection written under ``metadata["cost"]``, so the two
    agree exactly). ``attempt_metrics`` is accepted now to reserve the seam for
    Phase 8.4 value signals but is not consulted here.
    """
    graph = workspace.obligation_graph

    branch_direct: dict[str, CostVector] = {
        branch.branch_id: _branch_direct_cost(
            branch.branch_id, attempts, model_usage
        )
        for branch in workspace.branches
    }

    branches: list[BranchCostSummary] = []
    for branch in workspace.branches:
        direct = branch_direct[branch.branch_id]
        closure = _obligation_closure(branch.obligation_id, graph)
        helper_obligations = closure - {branch.obligation_id}
        transitive = direct
        for other in workspace.branches:
            if other.obligation_id in helper_obligations:
                transitive = add_cost(transitive, branch_direct[other.branch_id])
        branches.append(
            BranchCostSummary(
                branch_id=branch.branch_id,
                obligation_id=branch.obligation_id,
                direct_cost=direct,
                transitive_cost=transitive,
                attempts=sum(
                    1
                    for attempt in attempts
                    if _attempt_branch_id(attempt) == branch.branch_id
                ),
                accepted=branch.status == BranchStatus.ACCEPTED,
                blocked=branch.status == BranchStatus.BLOCKED,
                dormant=branch.status == BranchStatus.DORMANT,
            )
        )

    obligation_direct: dict[str, CostVector] = {}
    obligation_branches: dict[str, list[str]] = {}
    for branch in workspace.branches:
        obligation_direct.setdefault(branch.obligation_id, zero_cost())
        obligation_branches.setdefault(branch.obligation_id, [])
        obligation_branches[branch.obligation_id].append(branch.branch_id)
        obligation_direct[branch.obligation_id] = add_cost(
            obligation_direct[branch.obligation_id],
            branch_direct[branch.branch_id],
        )

    obligations: list[ObligationCostSummary] = []
    for obligation in graph.active():
        obligation_id = obligation.obligation_id
        direct = obligation_direct.get(obligation_id, zero_cost())
        closure = _obligation_closure(obligation_id, graph)
        helper_obligations = closure - {obligation_id}
        transitive = direct
        for helper_id in helper_obligations:
            transitive = add_cost(
                transitive, obligation_direct.get(helper_id, zero_cost())
            )
        obligations.append(
            ObligationCostSummary(
                obligation_id=obligation_id,
                direct_cost=direct,
                transitive_cost=transitive,
                branch_ids=tuple(sorted(obligation_branches.get(obligation_id, ()))),
            )
        )

    assembly: dict[str, Any] | None = None
    if assembly_outcome is not None and assembly_outcome.check_result is not None:
        assembly = to_dict(
            CostVector(
                checks=1,
                elapsed_ms=round(
                    assembly_outcome.check_result.elapsed_seconds * 1000
                ),
            )
        )

    run = to_dict(cost_vector_from_metrics_and_budget(run_metrics, snapshot))

    return {
        "branches": tuple(summary.to_dict() for summary in branches),
        "obligations": tuple(summary.to_dict() for summary in obligations),
        "assembly": assembly,
        "run": run,
    }
