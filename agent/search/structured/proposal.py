"""Typed structured action proposal protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ...proof_system.workspace import (
    DEFAULT_ALLOWED_MUTATIONS,
    SearchAction,
    SearchActionKind,
    search_action_from_dict,
)
from ..action import ActionGenerationRequest, ActionGenerator
from .proposal_types import (
    ALIGNMENT_RELATION_VALUES,
    PAYLOAD_KIND_CAPABILITY_TEST,
    PAYLOAD_KIND_CHANGE_REPRESENTATION,
    PAYLOAD_KIND_DECOMPOSE,
    PAYLOAD_KIND_IMPLEMENT,
    PAYLOAD_KIND_PROPOSE_ARGUMENT,
    PAYLOAD_KIND_REFINE_ARGUMENT,
    ActionPayload,
    AlignmentSpec,
    ArgumentStepSpec,
    CapabilityTestPayload,
    ChangeRepresentationPayload,
    DecomposeChildSpec,
    DecomposePayload,
    ImplementPayload,
    ProposeArgumentPayload,
    RefineArgumentPayload,
    alignment_spec_from_dict,
    argument_step_spec_from_dict,
    capability_test_payload_from_dict,
    change_representation_payload_from_dict,
    decompose_child_spec_from_dict,
    decompose_payload_from_dict,
    propose_argument_payload_from_dict,
    refine_argument_payload_from_dict,
)

SUPPORTED_PROPOSAL_KINDS: frozenset[SearchActionKind] = frozenset(
    {
        SearchActionKind.IMPLEMENT,
        SearchActionKind.REPAIR_IMPLEMENTATION,
        SearchActionKind.DECOMPOSE,
        SearchActionKind.RUN_CAPABILITY_TEST,
        SearchActionKind.PROPOSE_ARGUMENT,
        SearchActionKind.REFINE_ARGUMENT,
        SearchActionKind.CHANGE_REPRESENTATION,
    }
)


@dataclass(frozen=True)
class StructuredActionProposal:
    """One self-describing move the generator proposes."""

    action: SearchAction
    payload: ActionPayload
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> tuple[bool, tuple[str, ...]]:
        """Return ``(ok, errors)`` without raising."""
        errors: list[str] = []
        report = self.action.validate()
        if not report.ok:
            errors.extend(report.errors)

        kind = self.action.kind
        if kind not in SUPPORTED_PROPOSAL_KINDS:
            errors.append(f"unsupported proposal kind: {kind.value!r}")

        if kind in (
            SearchActionKind.IMPLEMENT,
            SearchActionKind.REPAIR_IMPLEMENTATION,
        ):
            if not isinstance(self.payload, ImplementPayload):
                errors.append(f"action kind {kind.value!r} requires ImplementPayload")
        elif kind is SearchActionKind.DECOMPOSE:
            if not isinstance(self.payload, DecomposePayload):
                errors.append("action kind 'decompose' requires DecomposePayload")
        elif kind is SearchActionKind.RUN_CAPABILITY_TEST:
            if not isinstance(self.payload, CapabilityTestPayload):
                errors.append(
                    "action kind 'run_capability_test' requires "
                    "CapabilityTestPayload"
                )
        elif kind is SearchActionKind.PROPOSE_ARGUMENT:
            if not isinstance(self.payload, ProposeArgumentPayload):
                errors.append(
                    "action kind 'propose_argument' requires ProposeArgumentPayload"
                )
            else:
                errors.extend(_argument_payload_errors(self.payload))
        elif kind is SearchActionKind.REFINE_ARGUMENT:
            if not isinstance(self.payload, RefineArgumentPayload):
                errors.append(
                    "action kind 'refine_argument' requires RefineArgumentPayload"
                )
            else:
                errors.extend(_argument_payload_errors(self.payload))
        elif kind is SearchActionKind.CHANGE_REPRESENTATION:
            if not isinstance(self.payload, ChangeRepresentationPayload):
                errors.append(
                    "action kind 'change_representation' requires "
                    "ChangeRepresentationPayload"
                )
            else:
                errors.extend(_representation_payload_errors(self.payload))

        return (not errors, tuple(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "payload": self.payload.to_dict(),
            "score": self.score,
            "metadata": dict(self.metadata),
        }


def structured_action_proposal_from_dict(
    data: dict[str, Any],
) -> StructuredActionProposal:
    action = search_action_from_dict(data["action"])
    payload_data = data["payload"]
    payload_kind = payload_data["kind"]
    if payload_kind == PAYLOAD_KIND_IMPLEMENT:
        payload: ActionPayload = ImplementPayload(
            proof_text=payload_data["proof_text"],
            source=payload_data.get("source", ""),
        )
    elif payload_kind == PAYLOAD_KIND_DECOMPOSE:
        payload = decompose_payload_from_dict(payload_data)
    elif payload_kind == PAYLOAD_KIND_CAPABILITY_TEST:
        payload = capability_test_payload_from_dict(payload_data)
    elif payload_kind == PAYLOAD_KIND_PROPOSE_ARGUMENT:
        payload = propose_argument_payload_from_dict(payload_data)
    elif payload_kind == PAYLOAD_KIND_REFINE_ARGUMENT:
        payload = refine_argument_payload_from_dict(payload_data)
    elif payload_kind == PAYLOAD_KIND_CHANGE_REPRESENTATION:
        payload = change_representation_payload_from_dict(payload_data)
    else:
        raise ValueError(f"unknown payload kind {payload_kind!r}")
    return StructuredActionProposal(
        action=action,
        payload=payload,
        score=data.get("score"),
        metadata=dict(data.get("metadata", {})),
    )


class StructuredActionGenerator(Protocol):
    """Generator that emits self-describing proposals."""

    _is_structured_generator: bool

    def generate(
        self, request: ActionGenerationRequest
    ) -> Sequence[StructuredActionProposal]:
        """Return one or more typed proposals for this request."""


LEGACY_KIND_DEFERRED = "_legacy_kind_deferred"
LEGACY_ACTION_KEY = "legacy_action"
FAILURE_HYPOTHESES_KEY = "failure_hypotheses"


class _LegacyActionGeneratorAdapter:
    """Adapt a legacy :class:`ActionGenerator` to the typed protocol."""

    def __init__(self, legacy: ActionGenerator) -> None:
        self._legacy = legacy

    def generate(
        self, request: ActionGenerationRequest
    ) -> Sequence[StructuredActionProposal]:
        proposals: list[StructuredActionProposal] = []
        for candidate in self._legacy.generate(request):
            metadata = dict(candidate.metadata)
            metadata[LEGACY_KIND_DEFERRED] = True
            metadata[LEGACY_ACTION_KEY] = candidate.action
            proposals.append(
                StructuredActionProposal(
                    action=SearchAction(
                        kind=SearchActionKind.IMPLEMENT,
                        target_branch_id=str(request.metadata.get("branch_id", "")),
                        allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                            SearchActionKind.IMPLEMENT
                        ],
                        rationale="",
                    ),
                    payload=ImplementPayload(
                        proof_text=candidate.proof_text,
                        source=candidate.proof_text,
                    ),
                    score=candidate.score,
                    metadata=metadata,
                )
            )
        return proposals


def adapt_legacy_generator(
    legacy: ActionGenerator | StructuredActionGenerator,
) -> StructuredActionGenerator:
    """Wrap a legacy :class:`ActionGenerator` as a typed generator."""
    if isinstance(legacy, _LegacyActionGeneratorAdapter):
        return legacy
    if getattr(legacy, "_is_structured_generator", False):
        return legacy  # type: ignore[return-value]
    return _LegacyActionGeneratorAdapter(legacy)


def _alignment_errors(spec: AlignmentSpec) -> tuple[str, ...]:
    errors: list[str] = []
    if not isinstance(spec.argument_step_id, str) or not spec.argument_step_id.strip():
        errors.append("alignment argument_step_id must be non-empty")
    if spec.relation not in ALIGNMENT_RELATION_VALUES:
        errors.append(f"unknown alignment relation {spec.relation!r}")
        return tuple(errors)
    has_target = any(
        target is not None
        for target in (
            spec.lean_declaration_id,
            spec.goal_fingerprint,
            spec.source_span,
        )
    )
    if spec.relation == "unaligned" and has_target:
        errors.append("unaligned alignment must not carry a Lean target")
    elif spec.relation != "unaligned" and not has_target:
        errors.append(f"alignment relation {spec.relation!r} requires a Lean target")
    return tuple(errors)


def _argument_payload_errors(
    payload: ProposeArgumentPayload | RefineArgumentPayload,
) -> tuple[str, ...]:
    return tuple(
        f"alignments[{index}]: {error}"
        for index, alignment in enumerate(payload.alignments)
        for error in _alignment_errors(alignment)
    )


def _representation_payload_errors(
    payload: ChangeRepresentationPayload,
) -> tuple[str, ...]:
    return tuple(
        f"alignments[{index}]: {error}"
        for index, alignment in enumerate(payload.alignments)
        for error in _alignment_errors(alignment)
    )
