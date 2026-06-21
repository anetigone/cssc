"""Competing failure hypotheses over collected evidence.

Phase 5 (``tmp/plan1.md`` §7-8) replaces the practice of routing one checker
failure to a single responsible layer with a step where the deterministic
parser turns raw results into neutral :class:`Observation` records (Phase 4),
and the model later interprets those observations into *multiple competing*
:class:`FailureHypothesis` records — each citing evidence, carrying a
confidence, naming affected argument steps, and proposing executable tests.

A hypothesis is a model product: it competes with siblings rather than
escalating up a fixed blame hierarchy. Repeated failure updates the
hypotheses; it does not force a single verdict. Infrastructure errors
(checker timeout / IO) are handled by deterministic rules and intentionally
have no place in :class:`FailureKind`.

This module ships only data + serialization + a deterministic validator. No
hypothesis is generated here — model-driven generation and the executor that
consumes hypotheses are Phase 6. The minimal loop never imports this package.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .action import SearchAction, search_action_from_dict


class FailureKind(str, Enum):
    """Semantic failure categories a model may compete on (``tmp/plan1.md`` §7).

    Closed set by design: adding a kind is a protocol change and should fail
    loudly on unknown values during deserialization. Infrastructure errors
    (checker timeout / IO) are excluded — they are handled by deterministic
    rules, not model-driven hypothesis revision.
    """

    THEOREM_MISUSE = "theorem_misuse"
    ARGUMENT_GAP = "argument_gap"
    INSUFFICIENT_ASSUMPTIONS = "insufficient_assumptions"
    ALIGNMENT_MISMATCH = "alignment_mismatch"
    IMPLEMENTATION_DEFECT = "implementation_defect"
    CAPABILITY_MISSING = "capability_missing"


@dataclass(frozen=True)
class FailureHypothesisReport:
    """Result of validating a :class:`FailureHypothesis`.

    Deterministic and never raises, mirroring :class:`SearchActionReport`.
    """

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


@dataclass(frozen=True)
class FailureHypothesis:
    """One competing explanation for a set of observations.

    ``evidence_ids`` cite the :class:`Observation` records this hypothesis
    rests on (must be non-empty — a hypothesis with no evidence is malformed).
    ``affected_step_ids`` may be empty (e.g. a capability gap need not pin a
    step). ``proposed_tests`` are :class:`SearchAction` records that could
    distinguish this hypothesis from its siblings; they may be empty until
    designed.
    """

    hypothesis_id: str
    kind: FailureKind
    confidence: float
    evidence_ids: tuple[str, ...] = ()
    affected_step_ids: tuple[str, ...] = ()
    proposed_tests: tuple[SearchAction, ...] = ()

    def validate(self) -> FailureHypothesisReport:
        """Check hypothesis invariants without raising.

        Verifies:

        * ``hypothesis_id`` is non-empty;
        * ``kind`` is a valid :class:`FailureKind`;
        * ``confidence`` is a finite float in ``[0.0, 1.0]``;
        * ``evidence_ids`` is non-empty, with non-empty unique ids;
        * ``affected_step_ids`` are non-empty unique ids (may be empty);
        * each ``proposed_tests`` entry is a valid :class:`SearchAction`
          (child errors are aggregated with a ``proposed_tests[i]:`` prefix).
        """
        errors: list[str] = []

        if not isinstance(self.hypothesis_id, str) or not self.hypothesis_id.strip():
            errors.append("failure hypothesis hypothesis_id must be non-empty")
        if not isinstance(self.kind, FailureKind):
            errors.append(f"unknown failure kind {self.kind!r}")

        if not isinstance(self.confidence, (int, float)) or isinstance(
            self.confidence, bool
        ):
            errors.append("confidence must be a number")
        elif isinstance(self.confidence, float) and not math.isfinite(self.confidence):
            errors.append("confidence must be finite")
        elif not (0 <= self.confidence <= 1):
            errors.append("confidence out of range [0.0, 1.0]")

        errors.extend(_check_id_tuple("evidence_ids", self.evidence_ids, required=True))
        errors.extend(
            _check_id_tuple("affected_step_ids", self.affected_step_ids, required=False)
        )

        for index, test in enumerate(self.proposed_tests):
            if not isinstance(test, SearchAction):
                errors.append(f"proposed_tests[{index}] is not a SearchAction")
                continue
            child = test.validate()
            if not child.ok:
                for child_error in child.errors:
                    errors.append(f"proposed_tests[{index}]: {child_error}")

        return FailureHypothesisReport(ok=not errors, errors=tuple(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "kind": self.kind.value,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "affected_step_ids": list(self.affected_step_ids),
            "proposed_tests": [test.to_dict() for test in self.proposed_tests],
        }


def failure_hypothesis_from_dict(data: dict[str, Any]) -> FailureHypothesis:
    return FailureHypothesis(
        hypothesis_id=data["hypothesis_id"],
        kind=FailureKind(data["kind"]),
        confidence=float(data["confidence"]),
        evidence_ids=tuple(data.get("evidence_ids", ())),
        affected_step_ids=tuple(data.get("affected_step_ids", ())),
        proposed_tests=tuple(
            search_action_from_dict(item) for item in data.get("proposed_tests", ())
        ),
    )


def _check_id_tuple(
    field: str,
    ids: tuple[str, ...],
    *,
    required: bool,
) -> tuple[str, ...]:
    """Validate a tuple of string ids: non-empty entries, no duplicates.

    ``required`` controls whether an empty tuple is an error (``evidence_ids``
    must be non-empty; ``affected_step_ids`` may be empty).
    """
    errors: list[str] = []
    if not ids:
        if required:
            errors.append(f"{field} must be non-empty")
        return tuple(errors)
    seen: set[str] = set()
    for value in ids:
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} contains an empty id")
            continue
        if value in seen:
            errors.append(f"duplicate {field} {value!r}")
        seen.add(value)
    return tuple(errors)
