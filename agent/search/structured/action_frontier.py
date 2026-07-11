"""Action-level frontier and proposal cache for opt-in Phase 9 scheduling.

Unlike the Phase 8 branch frontier, nodes here pin a concrete typed proposal
to the workspace version in which it was generated.  The cache is an
optimization only: the workspace remains authoritative and a node is never
silently rebound after a branch or obligation changes version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from agent.proof_system.workspace import BranchStatus, SearchActionKind

from .frontier_priority import legacy_priority_key
from .frontier_signals import is_ready, node_for
from .frontier_types import PriorityExplanation
from .proposal import StructuredActionProposal, structured_action_proposal_from_dict

if TYPE_CHECKING:
    from agent.proof_system.workspace import ProofBranch, ProofWorkspace


@dataclass(frozen=True)
class Estimate:
    """A scalar estimate reserved for the Phase 9 action-cost contract."""

    value: float

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("estimate value cannot be negative")

    def to_dict(self) -> dict[str, float]:
        return {"value": self.value}


def estimate_from_dict(data: dict[str, object] | None) -> Estimate | None:
    if data is None:
        return None
    return Estimate(float(data["value"]))


@dataclass(frozen=True)
class CostEstimate:
    """Comparable action execution-cost vector.

    Phase 9.1 uses frozen static priors. Phase 9.2 replaces those priors with
    history-derived values without changing the node/cache contract.
    """

    model_requests: Estimate | None = None
    input_tokens: Estimate | None = None
    output_tokens: Estimate | None = None
    billed_tokens: Estimate | None = None
    checks: Estimate | None = None
    checker_wall_ms: Estimate | None = None
    checker_cpu_ms: Estimate | None = None
    api_cost_usd: Estimate | None = None
    sample_count: int = 0
    source: str = "prior"
    estimator_version: str = "phase9.1-static-v1"

    def to_dict(self) -> dict[str, object]:
        return {
            name: value.to_dict() if value is not None else None
            for name, value in (
                ("model_requests", self.model_requests),
                ("input_tokens", self.input_tokens),
                ("output_tokens", self.output_tokens),
                ("billed_tokens", self.billed_tokens),
                ("checks", self.checks),
                ("checker_wall_ms", self.checker_wall_ms),
                ("checker_cpu_ms", self.checker_cpu_ms),
                ("api_cost_usd", self.api_cost_usd),
            )
        } | {
            "sample_count": self.sample_count,
            "source": self.source,
            "estimator_version": self.estimator_version,
        }


def cost_estimate_from_dict(data: dict[str, object]) -> CostEstimate:
    return CostEstimate(
        model_requests=estimate_from_dict(data.get("model_requests")),  # type: ignore[arg-type]
        input_tokens=estimate_from_dict(data.get("input_tokens")),  # type: ignore[arg-type]
        output_tokens=estimate_from_dict(data.get("output_tokens")),  # type: ignore[arg-type]
        billed_tokens=estimate_from_dict(data.get("billed_tokens")),  # type: ignore[arg-type]
        checks=estimate_from_dict(data.get("checks")),  # type: ignore[arg-type]
        checker_wall_ms=estimate_from_dict(data.get("checker_wall_ms")),  # type: ignore[arg-type]
        checker_cpu_ms=estimate_from_dict(data.get("checker_cpu_ms")),  # type: ignore[arg-type]
        api_cost_usd=estimate_from_dict(data.get("api_cost_usd")),  # type: ignore[arg-type]
        sample_count=int(data.get("sample_count", 0)),
        source=str(data.get("source", "prior")),
        estimator_version=str(data.get("estimator_version", "phase9.1-static-v1")),
    )


def static_execution_cost(kind: SearchActionKind) -> CostEstimate:
    """Frozen 9.1 prior: structural edits are free; checked moves cost one check."""
    checks = 0 if kind in {
        SearchActionKind.DECOMPOSE,
        SearchActionKind.PROPOSE_ARGUMENT,
        SearchActionKind.REFINE_ARGUMENT,
        SearchActionKind.CHANGE_REPRESENTATION,
    } else 1
    return CostEstimate(checks=Estimate(checks))


@dataclass(frozen=True)
class ActionFrontierNode:
    """One concrete, version-pinned action eligible for scheduling."""

    node_id: str
    branch_id: str
    obligation_id: str
    obligation_version: int
    proposal: StructuredActionProposal
    proposal_source: str
    proposal_batch_id: str | None
    proposal_model_tier: str | None
    cached_at_workspace_version: int
    estimated_execution_cost: CostEstimate
    priority_explanation: PriorityExplanation

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "branch_id": self.branch_id,
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "proposal": self.proposal.to_dict(),
            "proposal_source": self.proposal_source,
            "proposal_batch_id": self.proposal_batch_id,
            "proposal_model_tier": self.proposal_model_tier,
            "cached_at_workspace_version": self.cached_at_workspace_version,
            "estimated_execution_cost": self.estimated_execution_cost.to_dict(),
            "priority_explanation": self.priority_explanation.to_dict(),
        }


def action_frontier_node_from_dict(data: dict[str, object]) -> ActionFrontierNode:
    explanation = data["priority_explanation"]
    if not isinstance(explanation, dict):
        raise ValueError("action frontier node priority_explanation must be a dictionary")
    proposal = data["proposal"]
    estimate = data["estimated_execution_cost"]
    if not isinstance(proposal, dict) or not isinstance(estimate, dict):
        raise ValueError("action frontier node proposal and estimate must be dictionaries")
    return ActionFrontierNode(
        node_id=str(data["node_id"]), branch_id=str(data["branch_id"]),
        obligation_id=str(data["obligation_id"]), obligation_version=int(data["obligation_version"]),
        proposal=structured_action_proposal_from_dict(proposal), proposal_source=str(data["proposal_source"]),
        proposal_batch_id=data.get("proposal_batch_id") if isinstance(data.get("proposal_batch_id"), str) else None,
        proposal_model_tier=data.get("proposal_model_tier") if isinstance(data.get("proposal_model_tier"), str) else None,
        cached_at_workspace_version=int(data["cached_at_workspace_version"]),
        estimated_execution_cost=cost_estimate_from_dict(estimate),
        priority_explanation=PriorityExplanation(
            branch_id=str(explanation["branch_id"]), policy=str(explanation["policy"]),
            expected_incremental_cost=int(explanation["expected_incremental_cost"]),
            unlock_value=int(explanation["unlock_value"]),
            progress_likelihood=int(explanation["progress_likelihood"]),
            information_gain=int(explanation["information_gain"]),
            final_key_or_score=tuple(int(value) for value in explanation["final_key_or_score"]),  # type: ignore[index]
        ),
    )


@dataclass(frozen=True)
class ProposalCacheLimits:
    """Bound cache growth independently per branch and across the run."""

    per_branch_pending: int = 4
    global_pending: int = 16

    def __post_init__(self) -> None:
        if self.per_branch_pending < 1 or self.global_pending < 1:
            raise ValueError("proposal cache limits must be positive")


@dataclass(frozen=True)
class ProposalCache:
    """Immutable proposal cache; callers replace it after each lifecycle step."""

    entries: tuple[ActionFrontierNode, ...] = ()
    limits: ProposalCacheLimits = field(default_factory=ProposalCacheLimits)

    def add(
        self,
        workspace: ProofWorkspace,
        proposals: Iterable[StructuredActionProposal],
        *,
        proposal_source: str,
        proposal_batch_id: str | None = None,
        proposal_model_tier: str | None = None,
    ) -> tuple["ProposalCache", tuple[str, ...]]:
        """Cache valid proposals and return deterministic rejection reasons.

        No model call happens here.  A caller records cache miss/generation
        costs in the ledger before calling ``add``; cache hits therefore add no
        provider cost.
        """
        kept = list(self.valid_nodes(workspace))
        reasons: list[str] = []
        for proposal in proposals:
            if len(kept) >= self.limits.global_pending:
                reasons.append("global_pending_limit")
                break
            branch = _current_target_branch(workspace, proposal)
            if branch is None:
                reasons.append("invalid_or_stale_target")
                continue
            if sum(node.branch_id == branch.branch_id for node in kept) >= self.limits.per_branch_pending:
                reasons.append(f"per_branch_pending_limit:{branch.branch_id}")
                continue
            valid, validation_errors = proposal.validate()
            if not valid:
                reasons.append("invalid_proposal:" + "; ".join(validation_errors))
                continue
            node = _make_node(
                workspace, branch, proposal,
                proposal_source=proposal_source,
                proposal_batch_id=proposal_batch_id,
                proposal_model_tier=proposal_model_tier,
            )
            if any(existing.node_id == node.node_id for existing in kept):
                reasons.append("duplicate_proposal")
                continue
            kept.append(node)
        return ProposalCache(tuple(kept), self.limits), tuple(reasons)

    def valid_nodes(self, workspace: ProofWorkspace) -> tuple[ActionFrontierNode, ...]:
        """Drop stale/invalid nodes without rebinding them to new workspace state."""
        return tuple(node for node in self.entries if node_is_valid(node, workspace))

    def to_dict(self) -> dict[str, object]:
        return {
            "limits": {
                "per_branch_pending": self.limits.per_branch_pending,
                "global_pending": self.limits.global_pending,
            },
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def with_estimates(
        self, estimates_by_node_id: dict[str, CostEstimate]
    ) -> "ProposalCache":
        """Return a cache view with frozen history estimates applied.

        The supplied mapping is normally produced once from a Phase 9.2
        snapshot before selection.  Nodes absent from the map retain their
        static prior, making partial history coverage explicit rather than
        changing an unknown dimension to zero.
        """
        entries: list[ActionFrontierNode] = []
        for node in self.entries:
            estimate = estimates_by_node_id.get(node.node_id, node.estimated_execution_cost)
            checks = estimate.checks.value if estimate.checks is not None else 0
            entries.append(replace(
                node,
                estimated_execution_cost=estimate,
                priority_explanation=replace(
                    node.priority_explanation,
                    expected_incremental_cost=int(checks),
                ),
            ))
        return ProposalCache(tuple(entries), self.limits)

    def remove(self, node_id: str) -> "ProposalCache":
        """Return the cache without one consumed/rejected action node."""
        return ProposalCache(
            tuple(entry for entry in self.entries if entry.node_id != node_id),
            self.limits,
        )


def proposal_cache_from_dict(data: dict[str, object]) -> ProposalCache:
    limits = data.get("limits", {})
    entries = data.get("entries", ())
    if not isinstance(limits, dict) or not isinstance(entries, list):
        raise ValueError("proposal cache limits and entries have invalid shapes")
    return ProposalCache(
        entries=tuple(action_frontier_node_from_dict(entry) for entry in entries),  # type: ignore[arg-type]
        limits=ProposalCacheLimits(
            per_branch_pending=int(limits.get("per_branch_pending", 4)),
            global_pending=int(limits.get("global_pending", 16)),
        ),
    )


def node_is_valid(node: ActionFrontierNode, workspace: ProofWorkspace) -> bool:
    """Whether a cached action is still executable against this exact state."""
    if node.cached_at_workspace_version != workspace.version:
        return False
    branch = next((item for item in workspace.branches if item.branch_id == node.branch_id), None)
    if branch is None or branch.status is not BranchStatus.ACTIVE:
        return False
    if branch.obligation_id != node.obligation_id or branch.obligation_version != node.obligation_version:
        return False
    if node.proposal.action.target_branch_id != node.branch_id:
        return False
    if any(
        branch.argument.by_id(step_id) is None
        for step_id in node.proposal.action.target_step_ids
    ):
        return False
    return is_ready(branch, workspace)


class ActionFrontierPolicy(str, Enum):
    """Opt-in action schedulers; existing ``FrontierPolicy`` remains unchanged."""

    LEGACY = "legacy"
    COST_AWARE_V1 = "action_cost_aware_v1"


class ActionFrontier:
    """Deterministic scheduler over cached action nodes."""

    def __init__(self, *, policy: ActionFrontierPolicy = ActionFrontierPolicy.LEGACY) -> None:
        self.policy = policy
        self._pending: tuple[ActionFrontierNode, ...] = ()
        self._explanations: list[PriorityExplanation] = []

    def refresh(self, workspace: ProofWorkspace, cache: ProposalCache) -> ProposalCache:
        """Invalidate stale cache nodes and project the remaining ready actions."""
        valid = cache.valid_nodes(workspace)
        self._pending = valid
        return ProposalCache(valid, cache.limits)

    def has_work(self) -> bool:
        return bool(self._pending)

    def pop(self) -> ActionFrontierNode:
        if not self._pending:
            raise StopIteration("action frontier is empty")
        selected = min(self._pending, key=self._priority_key)
        self._pending = tuple(node for node in self._pending if node.node_id != selected.node_id)
        self._explanations.append(selected.priority_explanation)
        return selected

    def explanations(self) -> tuple[PriorityExplanation, ...]:
        return tuple(self._explanations)

    def _priority_key(self, node: ActionFrontierNode) -> tuple:
        branch_key = _branch_legacy_key(node)
        check_cost = node.estimated_execution_cost.checks.value if node.estimated_execution_cost.checks else float("inf")
        if self.policy is ActionFrontierPolicy.COST_AWARE_V1:
            return (check_cost, *branch_key, node.proposal.action.kind.value, node.node_id)
        return (*branch_key, node.proposal.action.kind.value, node.node_id)


def _current_target_branch(workspace: ProofWorkspace, proposal: StructuredActionProposal) -> ProofBranch | None:
    target = proposal.action.target_branch_id
    for branch in workspace.branches:
        if branch.branch_id == target and branch.status is BranchStatus.ACTIVE and is_ready(branch, workspace):
            return branch
    return None


def _make_node(
    workspace: ProofWorkspace,
    branch: ProofBranch,
    proposal: StructuredActionProposal,
    *,
    proposal_source: str,
    proposal_batch_id: str | None,
    proposal_model_tier: str | None,
) -> ActionFrontierNode:
    branch_node = node_for(branch, workspace)
    estimate = static_execution_cost(proposal.action.kind)
    explanation = PriorityExplanation(
        branch_id=branch.branch_id,
        policy=ActionFrontierPolicy.COST_AWARE_V1.value,
        expected_incremental_cost=int(estimate.checks.value) if estimate.checks else 0,
        unlock_value=branch_node.unlock_value,
        progress_likelihood=branch_node.progress_likelihood,
        information_gain=branch_node.information_gain,
        final_key_or_score=(),
    )
    fingerprint = json.dumps(proposal.to_dict(), sort_keys=True, ensure_ascii=False, default=str)
    node_id = hashlib.sha256(
        f"{branch.branch_id}|{branch.obligation_version}|{workspace.version}|{fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    return ActionFrontierNode(
        node_id=node_id,
        branch_id=branch.branch_id,
        obligation_id=branch.obligation_id,
        obligation_version=branch.obligation_version,
        proposal=proposal,
        proposal_source=proposal_source,
        proposal_batch_id=proposal_batch_id,
        proposal_model_tier=proposal_model_tier,
        cached_at_workspace_version=workspace.version,
        estimated_execution_cost=estimate,
        priority_explanation=explanation,
    )


def _branch_legacy_key(node: ActionFrontierNode) -> tuple:
    # The proposal's explanation is immutable, but branch statistics were used
    # when it was made.  This keeps replay deterministic without querying
    # future checker outcomes.
    explanation = node.priority_explanation
    return (
        0 if explanation.progress_likelihood else 1,
        -explanation.unlock_value,
        node.branch_id,
    )
