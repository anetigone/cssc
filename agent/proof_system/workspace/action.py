"""Unified search actions and their declared mutation scope.

Phase 5 (``tmp/plan1.md`` §7-8) represents every ProofAgent move — formalize,
decompose, argue, implement, repair — as one :class:`SearchAction` that
*declares which surfaces of the authoritative workspace it is allowed to
change* via :attr:`SearchAction.allowed_mutations`. If a repair uncovers that
the argument itself is wrong, the model must propose a *new* action rather than
silently rewrite the math steps inside one "repair": this is not inter-agent
permission isolation, it is so the search tree records "what changed" precisely.

Each :class:`SearchActionKind` carries a conservative default scope
(:data:`DEFAULT_ALLOWED_MUTATIONS`). An action may *narrow* below its default
but not *broaden* it; :meth:`SearchAction.validate` enforces that.

This module ships only data + serialization + a deterministic validator. No
action is ever executed here — the structured executor (frontier / AND-OR
search) is Phase 6. The minimal loop never imports this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class MutationKind(str, Enum):
    """The distinct mutable surfaces across the structured workspace state.

    These are exhaustive and disjoint. Observations are deliberately *not* a
    mutation kind: they are append-only evidence records, never mutated in
    place (a ``RUN_CHECK`` is read-only w.r.t. existing state; observations it
    produces are side-effects recorded by the executor).
    """

    FORMAL_SPECIFICATION = "formal_specification"
    OBLIGATION = "obligation"
    OBLIGATION_DEPENDENCY = "obligation_dependency"
    ARGUMENT_STEP = "argument_step"
    LEAN_ARTIFACT = "lean_artifact"
    ALIGNMENT_LINK = "alignment_link"
    BRANCH_STATUS = "branch_status"
    NEW_STRUCTURE = "new_structure"


class SearchActionKind(str, Enum):
    """The kinds of move a ProofAgent can make (``tmp/plan1.md`` §8)."""

    FORMALIZE = "formalize"
    DECOMPOSE = "decompose"
    PROPOSE_ARGUMENT = "propose_argument"
    REFINE_ARGUMENT = "refine_argument"
    IMPLEMENT = "implement"
    REPAIR_IMPLEMENTATION = "repair_implementation"
    CHANGE_REPRESENTATION = "change_representation"
    ADD_OBLIGATION = "add_obligation"
    REVISE_OBLIGATION = "revise_obligation"
    RUN_CHECK = "run_check"
    RUN_CAPABILITY_TEST = "run_capability_test"
    ASSEMBLE = "assemble"


#: Conservative default ``allowed_mutations`` per action kind.
#:
#: An action may pass a *narrower* scope than its default (silent), but
#: :meth:`SearchAction.validate` rejects any scope that *broadens* it, so a
#: cross-boundary change must be declared as a different ``SearchActionKind``.
#: Read-only actions (``RUN_CHECK`` / ``RUN_CAPABILITY_TEST`` / ``ASSEMBLE``)
#: carry an empty scope: they invoke subsystems over existing state and record
#: observations / assembly results as side-effects.
DEFAULT_ALLOWED_MUTATIONS: Mapping[
    SearchActionKind, tuple[MutationKind, ...]
] = MappingProxyType({
    SearchActionKind.FORMALIZE: (MutationKind.FORMAL_SPECIFICATION,),
    SearchActionKind.DECOMPOSE: (
        MutationKind.NEW_STRUCTURE,
        MutationKind.OBLIGATION_DEPENDENCY,
    ),
    SearchActionKind.PROPOSE_ARGUMENT: (
        MutationKind.ARGUMENT_STEP,
        MutationKind.ALIGNMENT_LINK,
    ),
    SearchActionKind.REFINE_ARGUMENT: (
        MutationKind.ARGUMENT_STEP,
        MutationKind.ALIGNMENT_LINK,
    ),
    SearchActionKind.IMPLEMENT: (
        MutationKind.LEAN_ARTIFACT,
        MutationKind.ALIGNMENT_LINK,
    ),
    # Per §8: a repair may touch the Lean artifact and its alignment mapping
    # only. Changing assumptions or math steps needs a new action.
    SearchActionKind.REPAIR_IMPLEMENTATION: (
        MutationKind.LEAN_ARTIFACT,
        MutationKind.ALIGNMENT_LINK,
    ),
    # The one explicitly broad action: a representation switch legitimately
    # crosses argument + Lean + alignment, so it is declared up front.
    SearchActionKind.CHANGE_REPRESENTATION: (
        MutationKind.ARGUMENT_STEP,
        MutationKind.LEAN_ARTIFACT,
        MutationKind.ALIGNMENT_LINK,
    ),
    SearchActionKind.ADD_OBLIGATION: (MutationKind.NEW_STRUCTURE,),
    SearchActionKind.REVISE_OBLIGATION: (
        MutationKind.OBLIGATION,
        MutationKind.OBLIGATION_DEPENDENCY,
    ),
    SearchActionKind.RUN_CHECK: (),
    SearchActionKind.RUN_CAPABILITY_TEST: (),
    SearchActionKind.ASSEMBLE: (),
})


@dataclass(frozen=True)
class SearchActionReport:
    """Result of validating a :class:`SearchAction`.

    Validation is deterministic and never raises, mirroring
    :class:`ArgumentGraphReport` / :class:`ObligationGraphReport`.
    """

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


@dataclass(frozen=True)
class SearchAction:
    """One ProofAgent move and its declared change scope.

    ``target_branch_id`` pins the action to a branch; ``target_step_ids`` names
    the argument steps it concerns (may be empty for non-argument actions).
    ``allowed_mutations`` declares which surfaces may change — it must be a
    subset of :data:`DEFAULT_ALLOWED_MUTATIONS` for ``kind``.
    """

    kind: SearchActionKind
    target_branch_id: str
    target_step_ids: tuple[str, ...] = ()
    allowed_mutations: tuple[MutationKind, ...] = ()
    rationale: str = ""

    def validate(self) -> SearchActionReport:
        """Check action invariants without raising.

        Verifies:

        * ``target_branch_id`` is a non-empty string;
        * ``rationale`` is non-empty (a scope-less move with no rationale is a
          malformed tree node);
        * ``allowed_mutations`` is a subset of the kind's default scope —
          narrowing is allowed, broadening is reported as an error;
        * ``allowed_mutations`` has no duplicates;
        * ``target_step_ids`` are non-empty strings without duplicates.

        ``target_step_ids`` are *not* cross-checked against an actual
        :class:`ArgumentGraph`: a :class:`SearchAction` is branch-agnostic, and
        cross-validation against live branch state is the structured executor's
        job (Phase 6).
        """
        errors: list[str] = []

        kind_is_valid = isinstance(self.kind, SearchActionKind)
        if not kind_is_valid:
            errors.append(f"unknown search action kind {self.kind!r}")
        if not isinstance(self.target_branch_id, str) or not self.target_branch_id.strip():
            errors.append("search action target_branch_id must be non-empty")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            errors.append("search action rationale must be non-empty")

        default_scope = DEFAULT_ALLOWED_MUTATIONS.get(self.kind, ()) if kind_is_valid else ()
        seen_mutations: set[MutationKind] = set()
        for mutation in self.allowed_mutations:
            if not isinstance(mutation, MutationKind):
                errors.append(f"unknown allowed mutation {mutation!r}")
                continue
            if mutation in seen_mutations:
                errors.append(
                    f"duplicate allowed mutation {mutation.value!r}"
                )
            seen_mutations.add(mutation)
            if mutation not in default_scope:
                kind_label = self.kind.value if kind_is_valid else repr(self.kind)
                errors.append(
                    f"action kind {kind_label} cannot allow mutation "
                    f"{mutation.value!r} (not in default scope)"
                )

        seen_steps: set[str] = set()
        for step_id in self.target_step_ids:
            if not isinstance(step_id, str) or not step_id.strip():
                errors.append("search action target_step_ids contains an empty id")
                continue
            if step_id in seen_steps:
                errors.append(f"duplicate target step id {step_id!r}")
            seen_steps.add(step_id)

        return SearchActionReport(ok=not errors, errors=tuple(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target_branch_id": self.target_branch_id,
            "target_step_ids": list(self.target_step_ids),
            "allowed_mutations": [m.value for m in self.allowed_mutations],
            "rationale": self.rationale,
        }


def search_action_from_dict(data: dict[str, Any]) -> SearchAction:
    return SearchAction(
        kind=SearchActionKind(data["kind"]),
        target_branch_id=data["target_branch_id"],
        target_step_ids=tuple(data.get("target_step_ids", ())),
        allowed_mutations=tuple(
            MutationKind(m) for m in data.get("allowed_mutations", ())
        ),
        rationale=data.get("rationale", ""),
    )
