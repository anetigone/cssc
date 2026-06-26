"""Structural reducer transitions for argument and representation state."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from ....proof_system.workspace import (
    AlignmentLink,
    AlignmentRelation,
    ArgumentGraph,
    ArgumentStep,
    BranchStatus,
    FailureHypothesis,
    ProofBranch,
    SearchAction,
    SearchActionKind,
)
from ....proof_system.workspace.workspace import ProofWorkspace
from ..proposal import AlignmentSpec, ArgumentStepSpec


def apply_argument(
    workspace: ProofWorkspace,
    action: SearchAction,
    *,
    branch_id: str,
    new_steps: Sequence[ArgumentStepSpec] = (),
    new_alignments: Sequence[AlignmentSpec] = (),
    refined_steps: Sequence[ArgumentStepSpec] = (),
    refined_alignments: Sequence[AlignmentSpec] = (),
) -> ProofWorkspace:
    """Fold a ``PROPOSE_ARGUMENT`` / ``REFINE_ARGUMENT`` action in place."""
    if action.kind not in (
        SearchActionKind.PROPOSE_ARGUMENT,
        SearchActionKind.REFINE_ARGUMENT,
    ):
        return workspace
    branch = _find_branch(workspace, branch_id)
    if branch is None or branch.status != BranchStatus.ACTIVE:
        return workspace

    if action.kind is SearchActionKind.PROPOSE_ARGUMENT:
        if not _alignments_cover(new_steps, new_alignments):
            return workspace
        if not _alignment_specs_valid(new_alignments):
            return workspace
        combined_steps = (
            *branch.argument.steps,
            *(_to_argument_step(spec) for spec in new_steps),
        )
        combined_alignments = (
            *branch.alignment,
            *(_to_alignment_link(spec) for spec in new_alignments),
        )
    else:
        if not _alignment_specs_valid(refined_alignments):
            return workspace
        existing_step_ids = {s.step_id for s in branch.argument.steps}
        existing_align_ids = {link.argument_step_id for link in branch.alignment}
        hits_steps = any(spec.step_id in existing_step_ids for spec in refined_steps)
        hits_alignments = any(
            spec.argument_step_id in existing_align_ids
            for spec in refined_alignments
        )
        if not hits_steps and not hits_alignments:
            return workspace
        replace_steps = {
            spec.step_id: _to_argument_step(spec) for spec in refined_steps
        }
        align_replacements = {
            spec.argument_step_id: _to_alignment_link(spec)
            for spec in refined_alignments
        }
        combined_steps = tuple(
            replace_steps.get(step.step_id, step)
            for step in branch.argument.steps
        )
        combined_alignments = tuple(
            align_replacements.get(link.argument_step_id, link)
            for link in branch.alignment
        )

    candidate = replace(
        branch,
        argument=ArgumentGraph(steps=combined_steps),
        alignment=combined_alignments,
        last_action=action,
    )
    if not candidate.validate().ok:
        return workspace
    return workspace.successor(branches=_replace_branch(workspace.branches, candidate))


def apply_change_representation(
    workspace: ProofWorkspace,
    action: SearchAction,
    *,
    branch_id: str,
    argument_steps: Sequence[ArgumentStepSpec],
    alignments: Sequence[AlignmentSpec],
) -> ProofWorkspace:
    """Fold a ``CHANGE_REPRESENTATION`` action by forking a branch."""
    if action.kind is not SearchActionKind.CHANGE_REPRESENTATION:
        return workspace
    parent = _find_branch(workspace, branch_id)
    if parent is None or parent.status != BranchStatus.ACTIVE:
        return workspace
    if not _alignments_cover(argument_steps, alignments):
        return workspace
    if not _alignment_specs_valid(alignments):
        return workspace

    child = ProofBranch(
        branch_id=_next_representation_branch_id(parent.branch_id, workspace.branches),
        obligation_id=parent.obligation_id,
        obligation_version=parent.obligation_version,
        parent_branch_id=parent.branch_id,
        argument=ArgumentGraph(
            steps=tuple(_to_argument_step(spec) for spec in argument_steps)
        ),
        alignment=tuple(_to_alignment_link(spec) for spec in alignments),
        observations=parent.observations,
        lean_artifact=None,
        status=BranchStatus.ACTIVE,
    )
    if not child.validate().ok:
        return workspace

    retired_parent = replace(parent, status=BranchStatus.SUPERSEDED)
    new_branches = (*_replace_branch(workspace.branches, retired_parent), child)
    return workspace.successor(branches=new_branches)


def apply_failure_hypotheses(
    workspace: ProofWorkspace,
    *,
    branch_id: str,
    hypotheses: Sequence[FailureHypothesis],
) -> ProofWorkspace:
    """Append competing failure hypotheses to a branch, dropping invalid ones."""
    branch = _find_branch(workspace, branch_id)
    if branch is None:
        return workspace

    observation_ids = {obs.observation_id for obs in branch.observations}
    step_ids = {step.step_id for step in branch.argument.steps}
    existing_ids = {hyp.hypothesis_id for hyp in branch.failure_hypotheses}

    accepted: list[FailureHypothesis] = []
    for hypothesis in hypotheses:
        if not hypothesis.validate().ok:
            continue
        if hypothesis.hypothesis_id in existing_ids:
            continue
        if not set(hypothesis.evidence_ids) <= observation_ids:
            continue
        if hypothesis.affected_step_ids and not set(
            hypothesis.affected_step_ids
        ) <= step_ids:
            continue
        if any(test.target_branch_id != branch_id for test in hypothesis.proposed_tests):
            continue
        existing_ids.add(hypothesis.hypothesis_id)
        accepted.append(hypothesis)

    if not accepted:
        return workspace
    updated = replace(
        branch,
        failure_hypotheses=(*branch.failure_hypotheses, *accepted),
    )
    return workspace.successor(branches=_replace_branch(workspace.branches, updated))


def _next_representation_branch_id(
    parent_branch_id: str, branches: tuple[ProofBranch, ...]
) -> str:
    prefix = f"{parent_branch_id}.rep"
    count = sum(1 for b in branches if b.branch_id.startswith(prefix))
    return f"{prefix}{count}"


def _to_argument_step(spec: ArgumentStepSpec) -> ArgumentStep:
    return ArgumentStep(
        step_id=spec.step_id,
        claim=spec.claim,
        justification=spec.justification,
        depends_on=tuple(spec.depends_on),
        introduced_fact_ids=tuple(spec.introduced_fact_ids),
        confidence=spec.confidence,
    )


def _to_alignment_link(spec: AlignmentSpec) -> AlignmentLink:
    relation = AlignmentRelation(spec.relation)
    if relation is AlignmentRelation.UNALIGNED:
        return AlignmentLink(argument_step_id=spec.argument_step_id, relation=relation)
    return AlignmentLink(
        argument_step_id=spec.argument_step_id,
        lean_declaration_id=spec.lean_declaration_id,
        goal_fingerprint=spec.goal_fingerprint,
        source_span=spec.source_span,
        relation=relation,
    )


def _alignments_cover(
    steps: Sequence[ArgumentStepSpec], alignments: Sequence[AlignmentSpec]
) -> bool:
    covered: set[str] = set()
    for spec in alignments:
        if spec.argument_step_id in covered:
            return False
        covered.add(spec.argument_step_id)
    return {step.step_id for step in steps} == covered


def _alignment_specs_valid(alignments: Sequence[AlignmentSpec]) -> bool:
    for spec in alignments:
        try:
            relation = AlignmentRelation(spec.relation)
        except ValueError:
            return False
        has_target = any(
            target is not None
            for target in (
                spec.lean_declaration_id,
                spec.goal_fingerprint,
                spec.source_span,
            )
        )
        if relation is AlignmentRelation.UNALIGNED and has_target:
            return False
        if relation is not AlignmentRelation.UNALIGNED and not has_target:
            return False
    return True


def _replace_branch(
    branches: tuple[ProofBranch, ...], updated: ProofBranch
) -> tuple[ProofBranch, ...]:
    replaced = tuple(
        updated if branch.branch_id == updated.branch_id else branch
        for branch in branches
    )
    if not any(branch.branch_id == updated.branch_id for branch in replaced):
        raise KeyError(f"branch {updated.branch_id!r} not present in workspace")
    return replaced


def _find_branch(workspace: ProofWorkspace, branch_id: str) -> ProofBranch | None:
    for branch in workspace.branches:
        if branch.branch_id == branch_id:
            return branch
    return None
