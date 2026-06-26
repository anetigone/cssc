"""Structured run result summary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from agent.proof_system.workspace import BranchStatus, ObligationStatus
from .solution_tracker import select_solution

if TYPE_CHECKING:
    from agent.proof_system.assembler import AssemblyResult
    from agent.proof_system.workspace import ProofWorkspace


@dataclass(frozen=True)
class ObligationSummary:
    """Summary for one active obligation."""

    obligation_id: str
    version: int
    status: str
    has_accepted_branch: bool
    branch_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "version": self.version,
            "status": self.status,
            "has_accepted_branch": self.has_accepted_branch,
            "branch_count": self.branch_count,
        }


def obligation_summary_from_dict(data: dict[str, Any]) -> ObligationSummary:
    return ObligationSummary(
        obligation_id=data["obligation_id"],
        version=int(data["version"]),
        status=data["status"],
        has_accepted_branch=bool(data["has_accepted_branch"]),
        branch_count=int(data["branch_count"]),
    )


@dataclass(frozen=True)
class BranchSummary:
    """Summary for one branch."""

    branch_id: str
    obligation_id: str
    obligation_version: int
    status: str
    parent_branch_id: str | None
    is_selected: bool
    has_artifact: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "status": self.status,
            "parent_branch_id": self.parent_branch_id,
            "is_selected": self.is_selected,
            "has_artifact": self.has_artifact,
        }


def branch_summary_from_dict(data: dict[str, Any]) -> BranchSummary:
    return BranchSummary(
        branch_id=data["branch_id"],
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        status=data["status"],
        parent_branch_id=data.get("parent_branch_id"),
        is_selected=bool(data["is_selected"]),
        has_artifact=bool(data["has_artifact"]),
    )


@dataclass(frozen=True)
class AssemblyOutcomeSummary:
    """Assembly outcome summary."""

    executed: bool
    accepted: bool
    errors: tuple[str, ...]
    has_check_result: bool
    safety_accepted: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed": self.executed,
            "accepted": self.accepted,
            "errors": list(self.errors),
            "has_check_result": self.has_check_result,
            "safety_accepted": self.safety_accepted,
        }


def assembly_outcome_summary_from_dict(
    data: dict[str, Any],
) -> AssemblyOutcomeSummary:
    return AssemblyOutcomeSummary(
        executed=bool(data.get("executed", False)),
        accepted=bool(data.get("accepted", False)),
        errors=tuple(data.get("errors", ())),
        has_check_result=bool(data.get("has_check_result", False)),
        safety_accepted=data.get("safety_accepted"),
    )


@dataclass(frozen=True)
class ResultSummary:
    """Terminal view of one structured run."""

    workspace_id: str
    workspace_version: int
    workspace_status: str
    validation_ok: bool
    validation_errors: tuple[str, ...]

    accepted_obligations: tuple[ObligationSummary, ...]
    open_obligations: tuple[ObligationSummary, ...]
    blocked_obligations: tuple[ObligationSummary, ...]

    selected_branch_ids: tuple[str, ...]
    selected_branches: tuple[BranchSummary, ...]
    preserved_alternatives: tuple[BranchSummary, ...]

    blocked_branch_obligation_ids: tuple[str, ...]

    assembly: AssemblyOutcomeSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_version": self.workspace_version,
            "workspace_status": self.workspace_status,
            "validation_ok": self.validation_ok,
            "validation_errors": list(self.validation_errors),
            "accepted_obligations": [item.to_dict() for item in self.accepted_obligations],
            "open_obligations": [item.to_dict() for item in self.open_obligations],
            "blocked_obligations": [item.to_dict() for item in self.blocked_obligations],
            "selected_branch_ids": list(self.selected_branch_ids),
            "selected_branches": [item.to_dict() for item in self.selected_branches],
            "preserved_alternatives": [
                item.to_dict() for item in self.preserved_alternatives
            ],
            "blocked_branch_obligation_ids": list(self.blocked_branch_obligation_ids),
            "assembly": self.assembly.to_dict(),
        }


_NOT_EXECUTED = AssemblyOutcomeSummary(
    executed=False,
    accepted=False,
    errors=(),
    has_check_result=False,
    safety_accepted=None,
)


def build_result_summary(
    workspace: ProofWorkspace,
    *,
    assembly_result: AssemblyResult | None = None,
    selected_branch_ids: Sequence[str] = (),
) -> ResultSummary:
    """Derive a summary for ``workspace``."""
    report = workspace.validate()
    active = workspace.obligation_graph.active()

    accepted_ids = {
        obligation.obligation_id
        for obligation in active
        if obligation.status == ObligationStatus.ACCEPTED
    }

    accepted_obs: list[ObligationSummary] = []
    open_obs: list[ObligationSummary] = []
    blocked_obs: list[ObligationSummary] = []
    for obligation in active:
        summary = _obligation_summary(workspace, obligation)
        if obligation.status == ObligationStatus.ACCEPTED:
            accepted_obs.append(summary)
        elif obligation.status == ObligationStatus.BLOCKED:
            blocked_obs.append(summary)
        else:  # OPEN / IN_PROGRESS
            open_obs.append(summary)

    selected_set = set(selected_branch_ids)
    if not selected_set and assembly_result is not None and assembly_result.accepted:
        selected_set = {
            branch.branch_id for branch in select_solution(workspace)
        }

    selected_branches: list[BranchSummary] = []
    preserved: list[BranchSummary] = []
    for branch in workspace.branches:
        summary = _branch_summary(branch, branch.branch_id in selected_set)
        if branch.branch_id in selected_set:
            selected_branches.append(summary)
        else:
            preserved.append(summary)

    blocked_branch_obligation_ids = tuple(
        sorted(
            {
                branch.obligation_id
                for branch in workspace.branches
                if branch.status == BranchStatus.BLOCKED
            }
            - accepted_ids
            - {
                obligation.obligation_id
                for obligation in active
                if obligation.status == ObligationStatus.BLOCKED
            }
        )
    )

    if assembly_result is None:
        assembly_summary = _NOT_EXECUTED
    else:
        safety = assembly_result.safety_verdict
        assembly_summary = AssemblyOutcomeSummary(
            executed=True,
            accepted=assembly_result.accepted,
            errors=assembly_result.errors,
            has_check_result=assembly_result.check_result is not None,
            safety_accepted=(safety.accepted if safety is not None else None),
        )

    return ResultSummary(
        workspace_id=workspace.workspace_id,
        workspace_version=workspace.version,
        workspace_status=workspace.status.value,
        validation_ok=report.ok,
        validation_errors=report.errors,
        accepted_obligations=tuple(accepted_obs),
        open_obligations=tuple(open_obs),
        blocked_obligations=tuple(blocked_obs),
        selected_branch_ids=tuple(item.branch_id for item in selected_branches),
        selected_branches=tuple(selected_branches),
        preserved_alternatives=tuple(preserved),
        blocked_branch_obligation_ids=blocked_branch_obligation_ids,
        assembly=assembly_summary,
    )


def _obligation_summary(
    workspace: ProofWorkspace, obligation: Any
) -> ObligationSummary:
    branches_for = [
        branch
        for branch in workspace.branches
        if branch.obligation_id == obligation.obligation_id
    ]
    has_accepted_branch = any(
        branch.obligation_version == obligation.version
        and branch.status == BranchStatus.ACCEPTED
        for branch in branches_for
    )
    return ObligationSummary(
        obligation_id=obligation.obligation_id,
        version=obligation.version,
        status=obligation.status.value,
        has_accepted_branch=has_accepted_branch,
        branch_count=len(branches_for),
    )


def _branch_summary(branch: Any, is_selected: bool) -> BranchSummary:
    return BranchSummary(
        branch_id=branch.branch_id,
        obligation_id=branch.obligation_id,
        obligation_version=branch.obligation_version,
        status=branch.status.value,
        parent_branch_id=branch.parent_branch_id,
        is_selected=is_selected,
        has_artifact=branch.lean_artifact is not None,
    )
