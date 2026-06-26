"""Deterministic structured workspace transition package."""
from __future__ import annotations

from .core import (
    REPAIR_THRESHOLD,
    StructuredActionResult,
    apply,
)
from .decompose import apply_decompose
from .structural import (
    apply_argument,
    apply_change_representation,
    apply_failure_hypotheses,
)

__all__ = [
    "REPAIR_THRESHOLD",
    "StructuredActionResult",
    "apply",
    "apply_argument",
    "apply_change_representation",
    "apply_decompose",
    "apply_failure_hypotheses",
]
