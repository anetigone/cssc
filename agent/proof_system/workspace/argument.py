"""Versionable mathematical argument steps and their DAG.

Phase 4 (``tmp/plan1.md`` §5/§6) lifts a natural-language proof sketch from an
opaque string into ordered, citeable steps. Each :class:`ArgumentStep` makes a
claim, justifies it, and declares which prior steps it depends on and which
facts it introduces. :class:`ArgumentGraph` validates that dependency structure
as a DAG, mirroring the deterministic no-raise style of
:func:`ObligationGraph.validate`.

The argument layer is proof-system-neutral: it carries no Lean text. Linking an
argument step to a Lean declaration or checker goal is the job of the Phase 4
alignment layer (``alignment.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArgumentStep:
    """One citeable step of a mathematical argument.

    ``depends_on`` lists step ids this step uses as premises (precedents in the
    argument); ``introduced_fact_ids`` names the facts/conclusions this step
    establishes and that later steps may cite. ``confidence`` is an optional
    model-assigned score; the structured layer records it but never promotes a
    confident-but-unverified claim into an accepted fact (that boundary lives
    in :class:`ProofWorkspace.register_accepted_fact`).
    """

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


def argument_step_from_dict(data: dict[str, Any]) -> ArgumentStep:
    return ArgumentStep(
        step_id=data["step_id"],
        claim=data["claim"],
        justification=data.get("justification", ""),
        depends_on=tuple(data.get("depends_on", ())),
        introduced_fact_ids=tuple(data.get("introduced_fact_ids", ())),
        confidence=data.get("confidence"),
    )


@dataclass(frozen=True)
class ArgumentGraphReport:
    """Result of validating an :class:`ArgumentGraph`.

    Validation is deterministic and never raises: structural problems are
    reported in :attr:`errors` with ``ok`` set to ``False``, matching
    :class:`ObligationGraphReport`.
    """

    ok: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


@dataclass(frozen=True)
class ArgumentGraph:
    """A DAG of mathematical argument steps for one proof strategy.

    Steps are stored in declaration order; the dependency edges
    (``step -> depends_on``) must form a DAG. A step may only depend on
    preceding reasoning captured elsewhere in the same graph — there is no
    cross-graph citation, since each argument belongs to one branch.
    """

    steps: tuple[ArgumentStep, ...] = ()

    def by_id(self, step_id: str) -> ArgumentStep | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def validate(self) -> ArgumentGraphReport:
        """Check argument invariants without raising.

        Verifies:

        * step ids are unique;
        * every ``depends_on`` target refers to an existing step;
        * the ``depends_on`` edges form a DAG (no cycles).

        Returns a report; ``ok`` is ``True`` iff ``errors`` is empty.
        """
        errors: list[str] = []

        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                errors.append(f"duplicate argument step id {step.step_id!r}")
            seen.add(step.step_id)

        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in seen:
                    errors.append(
                        f"argument step {step.step_id!r} depends on "
                        f"missing step {dependency!r}"
                    )

        cycle = _detect_step_cycle(self.steps)
        if cycle is not None:
            errors.append(
                f"argument dependency cycle detected: {' -> '.join(cycle)}"
            )

        return ArgumentGraphReport(ok=not errors, errors=tuple(errors))

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [step.to_dict() for step in self.steps]}


def argument_graph_from_dict(data: dict[str, Any]) -> ArgumentGraph:
    return ArgumentGraph(
        steps=tuple(argument_step_from_dict(item) for item in data.get("steps", ()))
    )


def _detect_step_cycle(steps: tuple[ArgumentStep, ...]) -> tuple[str, ...] | None:
    """Return a witness cycle path over ``depends_on`` edges, or ``None``.

    Mirrors the DFS three-colour marking used by
    :func:`agent.proof_system.workspace.graph._detect_cycle` so both DAG
    validators agree on behaviour.
    """
    by_id = {step.step_id: step for step in steps}
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {step_id: WHITE for step_id in by_id}
    stack: list[str] = []

    def visit(node_id: str) -> tuple[str, ...] | None:
        colour[node_id] = GREY
        stack.append(node_id)
        for dependency_id in by_id[node_id].depends_on:
            target = by_id.get(dependency_id)
            if target is None:
                continue
            state = colour[dependency_id]
            if state == GREY:
                start = stack.index(dependency_id)
                return tuple(stack[start:] + [dependency_id])
            if state == WHITE:
                found = visit(dependency_id)
                if found is not None:
                    return found
        stack.pop()
        colour[node_id] = BLACK
        return None

    for step_id in by_id:
        if colour[step_id] == WHITE:
            found = visit(step_id)
            if found is not None:
                return found
    return None
