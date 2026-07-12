"""A search branch: one proof strategy for one obligation.

The argument, Lean realization, alignment, evidence, and progress of one proof
strategy are tied into a single immutable record so that a failing Lean
implementation does not implicitly negate its mathematical strategy, and a new
attempt never overwrites a prior branch. Branches form a tree via
``parent_branch_id``: a local Lean repair or a change of mathematical strategy
spawns a child branch.

This module only defines the data and its serialization. Lifecycle transitions
(dormancy, superseding, eviction) and the frontier that selects among branches
live in the structured controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..base import ProgressSignal, progress_signal_from_dict
from .action import SearchAction, search_action_from_dict
from .alignment import AlignmentLink, AlignmentRelation, alignment_link_from_dict
from .argument import ArgumentGraph, argument_graph_from_dict
from .artifact import LeanArtifact, lean_artifact_from_dict
from .hypothesis import (
    FailureHypothesis,
    failure_hypothesis_from_dict,
)
from .observation import Observation, observation_from_dict


class BranchStatus(str, Enum):
    """Lifecycle state of one :class:`ProofBranch`."""

    ACTIVE = "active"
    DORMANT = "dormant"
    SUPERSEDED = "superseded"
    BLOCKED = "blocked"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class ProofBranchReport:
    """Deterministic result of validating one proof branch."""

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


@dataclass(frozen=True)
class ProofBranch:
    """One proof strategy for one obligation, carrying its full local state.

    ``obligation_id`` / ``obligation_version`` pin the branch to a specific
    obligation version; a branch must never silently attach to a revised
    obligation. ``last_action`` and ``failure_hypotheses`` remain in this
    authoritative record so mutation scope and evidence survive trace
    serialization. ``last_action_summary`` only preserves older trace data.
    """

    branch_id: str
    obligation_id: str
    obligation_version: int
    parent_branch_id: str | None = None
    argument: ArgumentGraph = field(default_factory=ArgumentGraph)
    lean_artifact: LeanArtifact | None = None
    alignment: tuple[AlignmentLink, ...] = ()
    observations: tuple[Observation, ...] = ()
    failure_hypotheses: tuple[FailureHypothesis, ...] = ()
    last_action: SearchAction | None = None
    # Retained for older traces. New state should use ``last_action``.
    last_action_summary: str | None = None
    progress: ProgressSignal = field(default_factory=ProgressSignal)
    status: BranchStatus = BranchStatus.ACTIVE

    def validate(self) -> ProofBranchReport:
        """Check branch-local pins and argument/alignment integrity."""
        errors = list(self.argument.validate().errors)

        if self.obligation_version < 1:
            errors.append(
                f"branch {self.branch_id!r} has invalid obligation version "
                f"{self.obligation_version}"
            )

        if self.lean_artifact is not None:
            artifact_pin = (
                self.lean_artifact.obligation_id,
                self.lean_artifact.obligation_version,
            )
            branch_pin = (self.obligation_id, self.obligation_version)
            if artifact_pin != branch_pin:
                errors.append(
                    f"branch {self.branch_id!r} artifact is pinned to "
                    f"{artifact_pin[0]!r} v{artifact_pin[1]}, not "
                    f"{branch_pin[0]!r} v{branch_pin[1]}"
                )

        step_ids = {step.step_id for step in self.argument.steps}
        aligned_step_ids: set[str] = set()
        for link in self.alignment:
            if link.argument_step_id not in step_ids:
                errors.append(
                    f"branch {self.branch_id!r} alignment references missing "
                    f"argument step {link.argument_step_id!r}"
                )
                continue
            aligned_step_ids.add(link.argument_step_id)
            has_target = any(
                target is not None
                for target in (
                    link.lean_declaration_id,
                    link.source_span,
                    link.goal_fingerprint,
                )
            )
            if link.relation == AlignmentRelation.UNALIGNED and has_target:
                errors.append(
                    f"branch {self.branch_id!r} marks step "
                    f"{link.argument_step_id!r} unaligned but supplies a Lean target"
                )
            elif link.relation != AlignmentRelation.UNALIGNED and not has_target:
                errors.append(
                    f"branch {self.branch_id!r} alignment for step "
                    f"{link.argument_step_id!r} has no Lean target"
                )

        for step_id in sorted(step_ids - aligned_step_ids):
            errors.append(
                f"branch {self.branch_id!r} argument step {step_id!r} has no "
                "alignment; record an explicit unaligned link"
            )

        observation_ids = {observation.observation_id for observation in self.observations}
        hypothesis_ids: set[str] = set()
        for hypothesis in self.failure_hypotheses:
            if hypothesis.hypothesis_id in hypothesis_ids:
                errors.append(
                    f"branch {self.branch_id!r} has duplicate failure hypothesis "
                    f"id {hypothesis.hypothesis_id!r}"
                )
            hypothesis_ids.add(hypothesis.hypothesis_id)
            errors.extend(
                f"hypothesis {hypothesis.hypothesis_id!r}: {error}"
                for error in hypothesis.validate().errors
            )
            for evidence_id in hypothesis.evidence_ids:
                if evidence_id not in observation_ids:
                    errors.append(
                        f"hypothesis {hypothesis.hypothesis_id!r} references "
                        f"missing observation {evidence_id!r}"
                    )
            for step_id in hypothesis.affected_step_ids:
                if step_id not in step_ids:
                    errors.append(
                        f"hypothesis {hypothesis.hypothesis_id!r} references "
                        f"missing argument step {step_id!r}"
                    )
            for test in hypothesis.proposed_tests:
                if test.target_branch_id != self.branch_id:
                    errors.append(
                        f"hypothesis {hypothesis.hypothesis_id!r} proposes a test "
                        f"for branch {test.target_branch_id!r}, not {self.branch_id!r}"
                    )

        if self.last_action is not None:
            errors.extend(
                f"last_action: {error}" for error in self.last_action.validate().errors
            )
            if self.last_action.target_branch_id != self.branch_id:
                errors.append(
                    f"branch {self.branch_id!r} last action targets branch "
                    f"{self.last_action.target_branch_id!r}"
                )
            for step_id in self.last_action.target_step_ids:
                if step_id not in step_ids:
                    errors.append(
                        f"branch {self.branch_id!r} last action references missing "
                        f"argument step {step_id!r}"
                    )

        return ProofBranchReport(ok=not errors, errors=tuple(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "parent_branch_id": self.parent_branch_id,
            "argument": self.argument.to_dict(),
            "lean_artifact": self.lean_artifact.to_dict() if self.lean_artifact else None,
            "alignment": [link.to_dict() for link in self.alignment],
            "observations": [obs.to_dict() for obs in self.observations],
            "failure_hypotheses": [
                hypothesis.to_dict() for hypothesis in self.failure_hypotheses
            ],
            "last_action": self.last_action.to_dict() if self.last_action else None,
            "last_action_summary": self.last_action_summary,
            "progress": self.progress.to_dict(),
            "status": self.status.value,
        }


def proof_branch_from_dict(data: dict[str, Any]) -> ProofBranch:
    artifact_data = data.get("lean_artifact")
    last_action_data = data.get("last_action")
    return ProofBranch(
        branch_id=data["branch_id"],
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        parent_branch_id=data.get("parent_branch_id"),
        argument=argument_graph_from_dict(data.get("argument", {}) or {}),
        lean_artifact=(
            lean_artifact_from_dict(artifact_data) if artifact_data else None
        ),
        alignment=tuple(
            alignment_link_from_dict(item) for item in data.get("alignment", ())
        ),
        observations=tuple(
            observation_from_dict(item) for item in data.get("observations", ())
        ),
        failure_hypotheses=tuple(
            failure_hypothesis_from_dict(item)
            for item in data.get("failure_hypotheses", ())
        ),
        last_action=(
            search_action_from_dict(last_action_data) if last_action_data else None
        ),
        last_action_summary=data.get("last_action_summary"),
        progress=progress_signal_from_dict(data.get("progress", {}) or {}),
        status=BranchStatus(data.get("status", BranchStatus.ACTIVE.value)),
    )
