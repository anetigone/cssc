"""Structured soft-budget hints (Phase 8.3).

A soft-budget hint is an observation derived purely from the workspace and the
global budget snapshot: an upper bound on the model calls / checks we *expect*
each active obligation to need before it should yield to a peer. Hints never
mutate the workspace, never call ``BudgetManager.reserve_*``, and never feed the
terminal status / stop-reason logic. They only

* drive the ``cost_aware_v2`` frontier ordering (overdraft branches are
  deprioritised, readiness stays a hard gate), and
* surface, alongside the Phase 8.1 cost summary, how much each obligation
  *borrowed* past its soft budget (``metadata["budget_hints"]``).

Like :mod:`agent.search.cost` and :mod:`agent.search.structured.costing`, this
module is a pure projection: it reads the public workspace surface only. Minimal
mode never imports it.

Dependency direction is one-way: this module imports per-branch / per-obligation
derivations from :mod:`agent.search.structured.frontier`; frontier never imports
back, so the hint envelope computed here for ordering is the single source of
truth for the ``cost_aware_v2`` key.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .frontier import (
    BudgetHintDefaults,
    soft_envelope_for_obligation,
)

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofWorkspace
    from agent.search.budget import BudgetSnapshot


@dataclass(frozen=True)
class ObligationBudgetHint:
    """Soft-budget envelope and realised borrowing for one obligation.

    ``soft_*`` are the expected envelope (derived from the obligation's
    unlock value, next action, stall, and accepted-neighbour state).
    ``borrowed_*`` are filled in by :func:`build_structured_result` from the
    Phase 8.1 cost summary: how far the obligation's realised direct cost
    overshot its envelope. Both default to zero so a freshly-derived hint
    (before the borrow join) is well-formed.
    """

    obligation_id: str
    soft_model_calls: int
    soft_checks: int
    borrowed_model_calls: int = 0
    borrowed_checks: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return {
            "obligation_id": self.obligation_id,
            "soft_model_calls": self.soft_model_calls,
            "soft_checks": self.soft_checks,
            "borrowed_model_calls": self.borrowed_model_calls,
            "borrowed_checks": self.borrowed_checks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ObligationBudgetHint":
        return cls(
            obligation_id=str(data["obligation_id"]),
            soft_model_calls=int(data.get("soft_model_calls", 0)),
            soft_checks=int(data.get("soft_checks", 0)),
            borrowed_model_calls=int(data.get("borrowed_model_calls", 0)),
            borrowed_checks=int(data.get("borrowed_checks", 0)),
        )


def build_obligation_budget_hints(
    workspace: ProofWorkspace,
    *,
    budget_snapshot: BudgetSnapshot,
    config: BudgetHintDefaults | None = None,
) -> tuple[ObligationBudgetHint, ...]:
    """Derive one :class:`ObligationBudgetHint` per active obligation.

    ``borrowed_*`` are left at zero here; :func:`build_structured_result` joins
    them from the Phase 8.1 cost summary after the run's direct cost is known.
    ``budget_snapshot`` is accepted (and the run's remaining budget is observed
    here) so callers can later cap envelopes against the global budget without
    a second pass; for now it shapes the contract, not the value.
    """
    del budget_snapshot  # reserved: future global-cap shaping (no hard cutoff)
    cfg = config or BudgetHintDefaults()
    graph = workspace.obligation_graph
    hints: list[ObligationBudgetHint] = []
    for obligation in graph.active():
        soft_checks, soft_model_calls = soft_envelope_for_obligation(
            obligation.obligation_id, workspace, cfg
        )
        hints.append(
            ObligationBudgetHint(
                obligation_id=obligation.obligation_id,
                soft_model_calls=soft_model_calls,
                soft_checks=soft_checks,
            )
        )
    return tuple(hints)


def join_borrowed_costs(
    hints: tuple[ObligationBudgetHint, ...],
    obligation_direct: dict[str, dict[str, int]],
) -> tuple[ObligationBudgetHint, ...]:
    """Fill ``borrowed_*`` from per-obligation direct cost entries.

    ``obligation_direct`` maps ``obligation_id`` to the serialised direct-cost
    dict (``{"checks": int, "model_calls": int, ...}``) produced by
    :func:`build_cost_summary` under ``cost_summary["obligations"]``. Missing
    entries default to zero cost, so unworked obligations borrow nothing.
    """
    updated: list[ObligationBudgetHint] = []
    for hint in hints:
        direct = obligation_direct.get(hint.obligation_id, {})
        borrowed_checks = max(
            0, int(direct.get("checks", 0)) - hint.soft_checks
        )
        borrowed_model_calls = max(
            0, int(direct.get("model_calls", 0)) - hint.soft_model_calls
        )
        updated.append(
            replace(
                hint,
                borrowed_checks=borrowed_checks,
                borrowed_model_calls=borrowed_model_calls,
            )
        )
    return tuple(updated)
