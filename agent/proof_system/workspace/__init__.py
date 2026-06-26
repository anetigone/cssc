"""Structured proof-search state primitives.

Phase 3 introduced the authoritative state for ``structured`` execution mode:
proof obligations, their acyclic dependency DAG, and the top-level workspace.
Phase 4 adds the argument/Lean alignment layer — versionable argument steps,
Lean artifacts, alignment links, checker observations, and the
:class:`ProofBranch` that ties them together — and wires branches into the
workspace. Phase 5 adds the unified action protocol and competing failure
hypotheses (:class:`SearchAction` / :class:`MutationKind` /
:class:`FailureHypothesis` / :class:`FailureKind`) that the structured
executor will eventually drive. These are proof-system-neutral frozen
dataclasses. The minimal loop never imports this package, so it pays no DAG,
branch, or action cost.
"""

from .action import (
    DEFAULT_ALLOWED_MUTATIONS,
    MutationKind,
    SearchAction,
    SearchActionKind,
    SearchActionReport,
    search_action_from_dict,
)
from .alignment import (
    AlignmentLink,
    AlignmentRelation,
    alignment_link_from_dict,
)
from .argument import (
    ArgumentGraph,
    ArgumentGraphReport,
    ArgumentStep,
    argument_graph_from_dict,
    argument_step_from_dict,
)
from .artifact import ArtifactKind, LeanArtifact, lean_artifact_from_dict
from .branch import (
    BranchStatus,
    ProofBranch,
    ProofBranchReport,
    proof_branch_from_dict,
)
from .graph import (
    ObligationGraph,
    ObligationGraphReport,
    obligation_graph_from_dict,
)
from .hypothesis import (
    FailureHypothesis,
    FailureHypothesisReport,
    FailureKind,
    failure_hypothesis_from_dict,
)
from .obligation import (
    ObligationStatus,
    ProofObligation,
    obligation_from_dict,
)
from .observation import (
    Observation,
    ObservationSource,
    observation_from_dict,
    observations_from_check_result,
)
from .spec import (
    FormalSpecification,
    VerifiedFact,
    WorkspaceStatus,
    formal_specification_from_dict,
    verified_fact_from_dict,
)
from .workspace import (
    ProofWorkspace,
    ProofWorkspaceReport,
    initialize_from_task,
    workspace_from_dict,
)

__all__ = [
    "AlignmentLink",
    "AlignmentRelation",
    "ArgumentGraph",
    "ArgumentGraphReport",
    "ArgumentStep",
    "ArtifactKind",
    "BranchStatus",
    "DEFAULT_ALLOWED_MUTATIONS",
    "FailureHypothesis",
    "FailureHypothesisReport",
    "FailureKind",
    "FormalSpecification",
    "LeanArtifact",
    "MutationKind",
    "Observation",
    "ObservationSource",
    "ObligationGraph",
    "ObligationGraphReport",
    "ObligationStatus",
    "ProofBranch",
    "ProofBranchReport",
    "ProofObligation",
    "ProofWorkspace",
    "ProofWorkspaceReport",
    "SearchAction",
    "SearchActionKind",
    "SearchActionReport",
    "VerifiedFact",
    "WorkspaceStatus",
    "alignment_link_from_dict",
    "argument_graph_from_dict",
    "argument_step_from_dict",
    "failure_hypothesis_from_dict",
    "formal_specification_from_dict",
    "initialize_from_task",
    "lean_artifact_from_dict",
    "obligation_from_dict",
    "obligation_graph_from_dict",
    "observation_from_dict",
    "observations_from_check_result",
    "proof_branch_from_dict",
    "search_action_from_dict",
    "verified_fact_from_dict",
    "workspace_from_dict",
]
