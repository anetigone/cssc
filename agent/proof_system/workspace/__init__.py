"""Structured proof-search state primitives.

Phase 3 introduces the authoritative state for ``structured`` execution mode:
proof obligations, their acyclic dependency DAG, and the top-level workspace.
These are proof-system-neutral frozen dataclasses. The minimal loop never
imports this package, so it pays no DAG cost.
"""

from .graph import (
    ObligationGraph,
    ObligationGraphReport,
    obligation_graph_from_dict,
)
from .obligation import (
    ObligationStatus,
    ProofObligation,
    obligation_from_dict,
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
    initialize_from_task,
    workspace_from_dict,
)

__all__ = [
    "FormalSpecification",
    "ObligationGraph",
    "ObligationGraphReport",
    "ObligationStatus",
    "ProofObligation",
    "ProofWorkspace",
    "VerifiedFact",
    "WorkspaceStatus",
    "initialize_from_task",
    "obligation_from_dict",
    "obligation_graph_from_dict",
    "formal_specification_from_dict",
    "verified_fact_from_dict",
    "workspace_from_dict",
]
