"""Lean realization of one proof obligation.

Phase 3 introduced a minimal :class:`LeanArtifact` inside ``assembler.py``
carrying only the source fragment and the obligation pin. Phase 4
(``tmp/plan1.md`` §5/§6) needs the branch layer to attach the artifact to a
mathematical argument, so the artifact gains identifiers a branch can align
against: the declaration id, the source span, and the proof body separated from
the surrounding declaration.

The class is relocated here from ``assembler.py`` so there is a single source
of truth; ``assembler.py`` now imports it. All new fields default so existing
assembly behaviour (which only reads ``source``) is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LeanArtifact:
    """The Lean realization of one accepted obligation.

    ``source`` is the complete declaration as fed to the checker. ``proof_body``
    is the proof part on its own, kept so an alignment link can point at the
    proof text without re-parsing. ``obligation_id`` and ``obligation_version``
    pin the artifact to a specific obligation version so a stale artifact can
    never silently attach to a revised obligation.
    """

    source: str
    obligation_id: str
    obligation_version: int
    declaration_id: str | None = None
    source_span: tuple[int, int] | None = None
    proof_body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "declaration_id": self.declaration_id,
            "source_span": list(self.source_span) if self.source_span else None,
            "proof_body": self.proof_body,
        }


def lean_artifact_from_dict(data: dict[str, Any]) -> LeanArtifact:
    span = data.get("source_span")
    return LeanArtifact(
        source=data["source"],
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        declaration_id=data.get("declaration_id"),
        source_span=tuple(span) if span else None,
        proof_body=data.get("proof_body", ""),
    )
