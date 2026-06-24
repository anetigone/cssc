"""Typed structured action proposal protocol (Phase 7.2).

Today the only thing an :class:`~agent.search.action.ActionGenerator` can emit
is an :class:`~agent.search.action.ActionCandidate` carrying a proof body. That
cannot express decomposition, capability probes, or argument edits. Phase 7.2
introduces a typed protocol — :class:`StructuredActionProposal` — that pairs a
self-describing :class:`~agent.proof_system.workspace.action.SearchAction`
(kind + change scope + rationale) with a typed :data:`ActionPayload` union.

Scope of *this* phase:

* :data:`SUPPORTED_PROPOSAL_KINDS` is deliberately small — ``IMPLEMENT`` /
  ``REPAIR_IMPLEMENTATION`` / ``DECOMPOSE`` / ``RUN_CAPABILITY_TEST`` — rather
  than all twelve :class:`SearchActionKind` values. The other kinds open up in
  later phases.
* Of these, the structured executor (Phase 6 / 7 controller) only *executes*
  ``IMPLEMENT`` / ``REPAIR_IMPLEMENTATION``. ``DECOMPOSE`` and
  ``RUN_CAPABILITY_TEST`` are defined, serialized, and validated types but are
  claimed by Phase 7.3 (capability audit) and 7.4 (decompose executor); the
  controller records them and skips execution.

The OLD :class:`ActionGenerator` keeps working unchanged via
:func:`adapt_legacy_generator`, which wraps every :class:`ActionCandidate` as an
``IMPLEMENT`` proposal. The minimal path never imports this module, so it bears
no cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, Union

from ...proof_system.workspace import (
    DEFAULT_ALLOWED_MUTATIONS,
    SearchAction,
    SearchActionKind,
    search_action_from_dict,
)
from ..action import ActionCandidate, ActionGenerationRequest, ActionGenerator

#: Discriminator strings for payload serialization. ``implement`` covers both
#: IMPLEMENT and REPAIR_IMPLEMENTATION (the kind distinction lives on the
#: enclosing :class:`SearchAction`, not on the payload).
PAYLOAD_KIND_IMPLEMENT = "implement"
PAYLOAD_KIND_DECOMPOSE = "decompose"
PAYLOAD_KIND_CAPABILITY_TEST = "run_capability_test"

#: The action kinds Phase 7.2 supports. Anything outside this set is rejected
#: at :meth:`StructuredActionProposal.validate` so the controller never sees an
#: unhandled kind.
SUPPORTED_PROPOSAL_KINDS: frozenset[SearchActionKind] = frozenset(
    {
        SearchActionKind.IMPLEMENT,
        SearchActionKind.REPAIR_IMPLEMENTATION,
        SearchActionKind.DECOMPOSE,
        SearchActionKind.RUN_CAPABILITY_TEST,
    }
)


@dataclass(frozen=True)
class ImplementPayload:
    """A proof body realizing the branch's current obligation.

    Covers both ``IMPLEMENT`` (first realization on a branch) and
    ``REPAIR_IMPLEMENTATION`` (a subsequent realization after a check failure):
    the kind distinction lives on the enclosing :class:`SearchAction`, the
    payload is just the text. ``source`` is kept separate from ``proof_text``
    because :class:`~agent.search.structured.reducer.StructuredActionResult`
    already distinguishes the human-readable body from the snippet the
    assembler renders; today both are equal (set by the adapter / controller).
    """

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
    """One child obligation proposed by a ``DECOMPOSE`` action.

    ``child_id`` is the generator-assigned stable id (the Phase 7.4 executor
    reconciles it against workspace obligation ids); ``statement`` is the Lean
    (or natural-language) statement of the sub-obligation; ``dependency_ids``
    names sibling/parent ids this child depends on — carried as data only in
    Phase 7.2 (the 7.4 executor builds the OBLIGATION_DEPENDENCY edges).
    """

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
    """Propose decomposition of the branch's obligation into children.

    Phase 7.2 only serializes and validates this; Phase 7.4 executes it.
    ``strategy`` is a free-form rationale string mirroring
    :attr:`SearchAction.rationale`, so the search tree records *why* this
    decomposition was proposed.
    """

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
    """Propose a capability probe before committing to an implementation.

    ``requirement`` is the capability being probed (e.g. ``tactic#simp``) and
    ``signature`` is a minimal Lean snippet exercising it. Phase 7.2 only
    serializes/validates; Phase 7.3 wires the audit. ``expected_outcome`` is a
    free-form note (the 7.3 audit decides accept/reject from the checker, not
    from this field), kept optional so 7.2 payloads stay minimal.
    """

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


ActionPayload = Union[ImplementPayload, DecomposePayload, CapabilityTestPayload]


@dataclass(frozen=True)
class StructuredActionProposal:
    """One self-describing move the generator proposes.

    The proposal carries its OWN :class:`SearchAction` (kind + scope +
    rationale); the controller no longer derives the kind from branch state. It
    validates ``action.allowed_mutations`` against the kind's default via
    :meth:`SearchAction.validate` and checks that ``action.kind`` agrees with
    the payload variant (an ``IMPLEMENT`` action pairs with
    :class:`ImplementPayload`, etc.).
    """

    action: SearchAction
    payload: ActionPayload
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> tuple[bool, tuple[str, ...]]:
        """Deterministic validation that never raises.

        Returns ``(ok, errors)``. Checks:

        * the action validates (delegates to :meth:`SearchAction.validate`,
          which enforces the kind's mutation scope);
        * the kind is in :data:`SUPPORTED_PROPOSAL_KINDS`;
        * the payload variant agrees with the kind.
        """
        errors: list[str] = []

        report = self.action.validate()
        if not report.ok:
            errors.extend(report.errors)

        kind = self.action.kind
        if kind not in SUPPORTED_PROPOSAL_KINDS:
            errors.append(
                f"unsupported proposal kind in Phase 7.2: {kind.value!r}"
            )

        if kind in (
            SearchActionKind.IMPLEMENT,
            SearchActionKind.REPAIR_IMPLEMENTATION,
        ):
            if not isinstance(self.payload, ImplementPayload):
                errors.append(
                    f"action kind {kind.value!r} requires ImplementPayload"
                )
        elif kind is SearchActionKind.DECOMPOSE:
            if not isinstance(self.payload, DecomposePayload):
                errors.append(
                    "action kind 'decompose' requires DecomposePayload"
                )
        elif kind is SearchActionKind.RUN_CAPABILITY_TEST:
            if not isinstance(self.payload, CapabilityTestPayload):
                errors.append(
                    "action kind 'run_capability_test' requires "
                    "CapabilityTestPayload"
                )

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
    else:
        raise ValueError(f"unknown payload kind {payload_kind!r}")
    return StructuredActionProposal(
        action=action,
        payload=payload,
        score=data.get("score"),
        metadata=dict(data.get("metadata", {})),
    )


class StructuredActionGenerator(Protocol):
    """Generator that emits self-describing :class:`StructuredActionProposal`.

    Native structured generators set the class attribute
    ``_is_structured_generator = True`` so the controller can distinguish them
    from a legacy :class:`ActionGenerator` (which it adapts via
    :func:`adapt_legacy_generator`) without a probe call.
    """

    _is_structured_generator: bool

    def generate(
        self, request: ActionGenerationRequest
    ) -> Sequence[StructuredActionProposal]:
        """Return one or more typed proposals for this request."""


#: Metadata key marking a proposal whose action kind is a placeholder to be
#: finalized by the controller once the candidate branch is materialized. The
#: legacy adapter cannot see branch state at ``generate`` time, so it emits
#: ``IMPLEMENT`` and the controller rewrites the kind from
#: ``branch.last_action`` — reproducing the old ``_pick_action`` rule exactly.
LEGACY_KIND_DEFERRED = "_legacy_kind_deferred"

#: Metadata key under which the adapter stashes the original candidate's
#: ``action`` string, so the controller can rebuild a :class:`CandidateEdit`
#: with the same ``action`` the legacy generator chose.
LEGACY_ACTION_KEY = "legacy_action"


class _LegacyActionGeneratorAdapter:
    """Adapt a legacy :class:`ActionGenerator` to the typed protocol.

    Every :class:`ActionCandidate` becomes an ``IMPLEMENT`` proposal with an
    :class:`ImplementPayload`. The action kind is a *placeholder* finalized by
    the controller (see :data:`LEGACY_KIND_DEFERRED`): the legacy generator
    cannot see branch state, and the IMPLEMENT-vs-REPAIR choice depends on the
    materialized candidate branch's ``last_action`` (only known after
    ``expand_candidate_branches``). Keeping the adapter branch-agnostic
    preserves the generator/controller boundary the protocol establishes.
    """

    def __init__(self, legacy: ActionGenerator) -> None:
        self._legacy = legacy

    def generate(
        self, request: ActionGenerationRequest
    ) -> Sequence[StructuredActionProposal]:
        candidates = self._legacy.generate(request)
        proposals: list[StructuredActionProposal] = []
        for candidate in candidates:
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
                        rationale="",  # finalized by the controller
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
    """Wrap a legacy :class:`ActionGenerator` as a typed generator.

    Idempotent: passing an already-adapted generator returns it unchanged, so
    the controller can call this defensively without producing nested adapters.
    A generator that already declares ``_is_structured_generator`` is returned
    unchanged too.
    """
    if isinstance(legacy, _LegacyActionGeneratorAdapter):
        return legacy
    if getattr(legacy, "_is_structured_generator", False):
        return legacy  # type: ignore[return-value]
    return _LegacyActionGeneratorAdapter(legacy)
