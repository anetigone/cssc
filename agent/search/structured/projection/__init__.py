"""Structured workspace context projection.

The projection is a pure derivation over a :class:`ProofWorkspace` plus a
``branch_id``. It produces a prompt-renderable dict without importing any
prompt/renderer code, and the shared renderer in :mod:`agent.agents.proof`
duck-types the dict via ``Mapping``/``Sequence`` so it never needs to import
this package.
"""

from __future__ import annotations

from .core import (
    StructuredContextProjection,
    build_context_projection,
    context_projection_from_dict,
)
from .slots import (
    MAX_PROJECTED_OBSERVATIONS,
    MAX_SIBLING_BRANCHES,
    AcceptedFactSlot,
    ArgumentStepSlot,
    DependencyFact,
    FailureHypothesisSlot,
    ObservationSlot,
    ObligationSlot,
    SiblingBranchSlot,
)

__all__ = [
    "MAX_PROJECTED_OBSERVATIONS",
    "MAX_SIBLING_BRANCHES",
    "AcceptedFactSlot",
    "ArgumentStepSlot",
    "DependencyFact",
    "FailureHypothesisSlot",
    "ObservationSlot",
    "ObligationSlot",
    "SiblingBranchSlot",
    "StructuredContextProjection",
    "build_context_projection",
    "context_projection_from_dict",
]
