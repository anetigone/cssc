"""Structured checker observations and the deterministic extractor.

Rather than routing a single checker failure to one responsible layer, a
neutral translation step turns a raw checker result into one or more
:class:`Observation` records, which the model later interprets into competing
failure hypotheses. The observation layer records only facts (category,
declaration id, goal fingerprint, the attempt that produced it); it never
infers blame.

``category`` is stored as a plain string rather than the
:class:`DiagnosticCategory` enum so that new categories introduced by future
parsers do not break deserialization of older traces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..base import CheckResult


class ObservationSource(str, Enum):
    """Which subsystem produced an observation."""

    CHECKER = "checker"
    RETRIEVER = "retriever"
    CAPABILITY_AUDIT = "capability_audit"


@dataclass(frozen=True)
class Observation:
    """One neutral, citeable piece of evidence.

    ``raw_evidence_ref`` is a string anchor (e.g. ``"attempt:3"``) pointing back
    to the attempt that produced it; the full raw checker log lives in trace,
    not in the observation. ``declaration_id`` and ``goal_fingerprint`` carry
    the alignment hooks that error attribution relies on.
    """

    observation_id: str
    source: ObservationSource
    category: str
    message: str = ""
    declaration_id: str | None = None
    goal_fingerprint: str | None = None
    raw_evidence_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source": self.source.value,
            "category": self.category,
            "message": self.message,
            "declaration_id": self.declaration_id,
            "goal_fingerprint": self.goal_fingerprint,
            "raw_evidence_ref": self.raw_evidence_ref,
        }


def observation_from_dict(data: dict[str, Any]) -> Observation:
    return Observation(
        observation_id=data["observation_id"],
        source=ObservationSource(data.get("source", ObservationSource.CHECKER.value)),
        category=data["category"],
        message=data.get("message", ""),
        declaration_id=data.get("declaration_id"),
        goal_fingerprint=data.get("goal_fingerprint"),
        raw_evidence_ref=data.get("raw_evidence_ref", ""),
    )


def observations_from_check_result(
    check_result: CheckResult,
    attempt_index: int,
) -> tuple[Observation, ...]:
    """Translate a checker result into neutral observations.

    Each unsolved/sorry goal becomes one observation pinned to
    ``attempt:<attempt_index>``. A checker-accepted result yields no
    observations: there is no failure evidence to attribute. When the parsed
    feedback carries no goals, a single summary observation records the
    category and message so a non-accepted result is never silently dropped.

    Deterministic and never raises: a missing ``parsed_feedback`` falls back to
    a single category/message observation.
    """
    if check_result.accepted:
        return ()

    evidence_ref = f"attempt:{attempt_index}"
    feedback = check_result.parsed_feedback
    category = (
        feedback.category.value if feedback is not None else check_result.category.value
    )
    message = feedback.message if feedback is not None and feedback.message else (
        check_result.raw_output.strip()[:160] if check_result.raw_output else ""
    )

    if feedback is not None and feedback.goal_state:
        observations: list[Observation] = []
        for index, goal in enumerate(feedback.goal_state):
            observations.append(
                Observation(
                    observation_id=f"{evidence_ref}:goal:{index}",
                    source=ObservationSource.CHECKER,
                    category=category,
                    message=goal.text.strip()[:160],
                    declaration_id=goal.declaration_id,
                    goal_fingerprint=goal.goal_fingerprint or None,
                    raw_evidence_ref=evidence_ref,
                )
            )
        return tuple(observations)

    return (
        Observation(
            observation_id=f"{evidence_ref}:summary",
            source=ObservationSource.CHECKER,
            category=category,
            message=message,
            raw_evidence_ref=evidence_ref,
        ),
    )
