"""Mutable frontier scheduler for structured search.

This module stays the compatibility import surface for frontier-related types
while the pure projections and priority keys live in smaller sibling modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .frontier_priority import PriorityKey, select_priority_key
from .frontier_signals import is_ready, node_for, soft_envelope_for_obligation
from .frontier_types import (
    STALL_THRESHOLD,
    BudgetHintDefaults,
    FrontierNode,
    FrontierPolicy,
    PriorityExplanation,
)

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofWorkspace


class Frontier:
    """Mutable scheduler for ready branches."""

    def __init__(
        self, *, policy: FrontierPolicy = FrontierPolicy.LEGACY
    ) -> None:
        self._policy = policy
        self._pending: list[FrontierNode] = []
        self._pending_ids: set[str] = set()
        self._popped_this_round: set[str] = set()
        self._explanations: list[PriorityExplanation] = []

    def seed(self, workspace: ProofWorkspace) -> None:
        """Load all ready branches of the workspace as pending nodes."""
        self._pending = []
        self._pending_ids = set()
        self._popped_this_round = set()
        for branch in workspace.branches:
            if is_ready(branch, workspace):
                node = node_for(branch, workspace)
                self._pending.append(node)
                self._pending_ids.add(node.branch_id)

    def has_work(self) -> bool:
        """True iff at least one pending node remains."""
        return bool(self._pending)

    @property
    def policy(self) -> FrontierPolicy:
        """The priority policy this frontier orders ready branches by."""
        return self._policy

    def pop(self) -> FrontierNode:
        """Return and remove the highest-priority pending node."""
        if not self._pending:
            raise StopIteration("frontier is empty")
        key = select_priority_key(self._policy)
        self._pending.sort(key=key)
        node = self._pending.pop(0)
        self._pending_ids.discard(node.branch_id)
        self._popped_this_round.add(node.branch_id)
        self._record_explanation(node, key)
        return node

    def _record_explanation(self, node: FrontierNode, key: PriorityKey) -> None:
        """Append a deterministic per-pop explanation for the trace."""
        full_key = key(node)
        int_prefix = tuple(part for part in full_key if isinstance(part, int))
        self._explanations.append(
            PriorityExplanation(
                branch_id=node.branch_id,
                policy=self._policy.value,
                expected_incremental_cost=node.next_action_cost,
                unlock_value=node.unlock_value,
                progress_likelihood=node.progress_likelihood,
                information_gain=node.information_gain,
                final_key_or_score=int_prefix,
            )
        )

    def explanations(self) -> tuple[PriorityExplanation, ...]:
        """All per-pop explanations recorded since construction, in pop order."""
        return tuple(self._explanations)

    def update(
        self,
        workspace: ProofWorkspace,
        branch_id: str,
        accepted: bool,
        *,
        attempted_branch_ids: tuple[str, ...] = (),
    ) -> None:
        """Refresh the pending set after a reducer transition."""
        del branch_id, accepted  # status in the workspace is authoritative
        self._popped_this_round.update(attempted_branch_ids)

        ready = [branch for branch in workspace.branches if is_ready(branch, workspace)]
        eligible = [
            branch for branch in ready if branch.branch_id not in self._popped_this_round
        ]
        if not eligible and ready:
            self._popped_this_round.clear()
            eligible = ready

        fresh = [node_for(branch, workspace) for branch in eligible]
        self._pending = fresh
        self._pending_ids = {node.branch_id for node in fresh}


__all__ = [
    "STALL_THRESHOLD",
    "BudgetHintDefaults",
    "Frontier",
    "FrontierNode",
    "FrontierPolicy",
    "PriorityExplanation",
    "soft_envelope_for_obligation",
]
