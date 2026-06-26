"""Payload dataclasses for typed structured action proposals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

PAYLOAD_KIND_IMPLEMENT = "implement"
PAYLOAD_KIND_DECOMPOSE = "decompose"
PAYLOAD_KIND_CAPABILITY_TEST = "run_capability_test"
PAYLOAD_KIND_PROPOSE_ARGUMENT = "propose_argument"
PAYLOAD_KIND_REFINE_ARGUMENT = "refine_argument"
PAYLOAD_KIND_CHANGE_REPRESENTATION = "change_representation"


@dataclass(frozen=True)
class ImplementPayload:
    """A proof body realizing the branch's current obligation."""

    proof_text: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PAYLOAD_KIND_IMPLEMENT,
            "proof_text": self.proof_text,
            "source": self.source,
        }


@dataclass(frozen=True)
class DecomposeChildSpec:
    """One child obligation proposed by a ``DECOMPOSE`` action."""

    child_id: str
    statement: str
    dependency_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "statement": self.statement,
            "dependency_ids": list(self.dependency_ids),
        }


def decompose_child_spec_from_dict(data: dict[str, Any]) -> DecomposeChildSpec:
    return DecomposeChildSpec(
        child_id=data["child_id"],
        statement=data["statement"],
        dependency_ids=tuple(data.get("dependency_ids", ())),
    )


@dataclass(frozen=True)
class DecomposePayload:
    """Propose decomposition of the branch's obligation into children."""

    children: tuple[DecomposeChildSpec, ...]
    strategy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PAYLOAD_KIND_DECOMPOSE,
            "children": [child.to_dict() for child in self.children],
            "strategy": self.strategy,
        }


def decompose_payload_from_dict(data: dict[str, Any]) -> DecomposePayload:
    return DecomposePayload(
        children=tuple(
            decompose_child_spec_from_dict(child)
            for child in data.get("children", ())
        ),
        strategy=data.get("strategy", ""),
    )


@dataclass(frozen=True)
class CapabilityTestPayload:
    """A minimal Lean snippet probing an environment capability."""

    requirement: str
    signature: str
    expected_outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PAYLOAD_KIND_CAPABILITY_TEST,
            "requirement": self.requirement,
            "signature": self.signature,
            "expected_outcome": self.expected_outcome,
        }


def capability_test_payload_from_dict(data: dict[str, Any]) -> CapabilityTestPayload:
    return CapabilityTestPayload(
        requirement=data["requirement"],
        signature=data["signature"],
        expected_outcome=data.get("expected_outcome", ""),
    )


@dataclass(frozen=True)
class ArgumentStepSpec:
    """A serializable description of one mathematical argument step."""

    step_id: str
    claim: str
    justification: str = ""
    depends_on: tuple[str, ...] = ()
    introduced_fact_ids: tuple[str, ...] = ()
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "claim": self.claim,
            "justification": self.justification,
            "depends_on": list(self.depends_on),
            "introduced_fact_ids": list(self.introduced_fact_ids),
            "confidence": self.confidence,
        }


def argument_step_spec_from_dict(data: dict[str, Any]) -> ArgumentStepSpec:
    return ArgumentStepSpec(
        step_id=data["step_id"],
        claim=data["claim"],
        justification=data.get("justification", ""),
        depends_on=tuple(data.get("depends_on", ())),
        introduced_fact_ids=tuple(data.get("introduced_fact_ids", ())),
        confidence=data.get("confidence"),
    )


ALIGNMENT_RELATION_VALUES: frozenset[str] = frozenset(
    {"implements", "partial", "unaligned"}
)


@dataclass(frozen=True)
class AlignmentSpec:
    """A serializable description of one alignment link."""

    argument_step_id: str
    relation: str = "unaligned"
    lean_declaration_id: str | None = None
    goal_fingerprint: str | None = None
    source_span: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argument_step_id": self.argument_step_id,
            "relation": self.relation,
            "lean_declaration_id": self.lean_declaration_id,
            "goal_fingerprint": self.goal_fingerprint,
            "source_span": list(self.source_span) if self.source_span else None,
        }


def alignment_spec_from_dict(data: dict[str, Any]) -> AlignmentSpec:
    span = data.get("source_span")
    return AlignmentSpec(
        argument_step_id=data["argument_step_id"],
        relation=data.get("relation", "unaligned"),
        lean_declaration_id=data.get("lean_declaration_id"),
        goal_fingerprint=data.get("goal_fingerprint"),
        source_span=tuple(span) if span else None,
    )


@dataclass(frozen=True)
class ProposeArgumentPayload:
    """Append new argument steps and their alignments to the branch."""

    steps: tuple[ArgumentStepSpec, ...]
    alignments: tuple[AlignmentSpec, ...]
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PAYLOAD_KIND_PROPOSE_ARGUMENT,
            "steps": [step.to_dict() for step in self.steps],
            "alignments": [alignment.to_dict() for alignment in self.alignments],
            "rationale": self.rationale,
        }


def propose_argument_payload_from_dict(
    data: dict[str, Any],
) -> ProposeArgumentPayload:
    return ProposeArgumentPayload(
        steps=tuple(
            argument_step_spec_from_dict(item) for item in data.get("steps", ())
        ),
        alignments=tuple(
            alignment_spec_from_dict(item) for item in data.get("alignments", ())
        ),
        rationale=data.get("rationale", ""),
    )


@dataclass(frozen=True)
class RefineArgumentPayload:
    """Replace existing argument steps and their alignments in place."""

    steps: tuple[ArgumentStepSpec, ...]
    alignments: tuple[AlignmentSpec, ...]
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PAYLOAD_KIND_REFINE_ARGUMENT,
            "steps": [step.to_dict() for step in self.steps],
            "alignments": [alignment.to_dict() for alignment in self.alignments],
            "rationale": self.rationale,
        }


def refine_argument_payload_from_dict(
    data: dict[str, Any],
) -> RefineArgumentPayload:
    return RefineArgumentPayload(
        steps=tuple(
            argument_step_spec_from_dict(item) for item in data.get("steps", ())
        ),
        alignments=tuple(
            alignment_spec_from_dict(item) for item in data.get("alignments", ())
        ),
        rationale=data.get("rationale", ""),
    )


@dataclass(frozen=True)
class ChangeRepresentationPayload:
    """Fork a new representation branch with a replacement argument layer."""

    argument: tuple[ArgumentStepSpec, ...]
    alignments: tuple[AlignmentSpec, ...]
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PAYLOAD_KIND_CHANGE_REPRESENTATION,
            "argument": [step.to_dict() for step in self.argument],
            "alignments": [alignment.to_dict() for alignment in self.alignments],
            "rationale": self.rationale,
        }


def change_representation_payload_from_dict(
    data: dict[str, Any],
) -> ChangeRepresentationPayload:
    return ChangeRepresentationPayload(
        argument=tuple(
            argument_step_spec_from_dict(item) for item in data.get("argument", ())
        ),
        alignments=tuple(
            alignment_spec_from_dict(item) for item in data.get("alignments", ())
        ),
        rationale=data.get("rationale", ""),
    )


ActionPayload = Union[
    ImplementPayload,
    DecomposePayload,
    CapabilityTestPayload,
    ProposeArgumentPayload,
    RefineArgumentPayload,
    ChangeRepresentationPayload,
]
