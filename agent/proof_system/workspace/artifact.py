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
from enum import Enum
from typing import Any


class ArtifactKind(str, Enum):
    """How an artifact renders into the assembled source.

    ``PROOF_BODY`` is a snippet that fills the task's single proof hole — the
    artifact shape for a *root* obligation, whose Lean statement already lives
    in the task template. ``DECLARATION`` is a standalone ``def``/``lemma``
    declaration that the assembler emits as its own top-level statement — the
    shape for a *helper* (decomposed child) obligation, which has no pre-existing
    slot in the template.

    The single-root baseline only ever produces ``PROOF_BODY``; the default keeps
    every existing construction unchanged. Multi-obligation assembly (Phase 7.4)
    is where ``DECLARATION`` first appears.
    """

    PROOF_BODY = "proof_body"
    DECLARATION = "declaration"


@dataclass(frozen=True)
class LeanArtifact:
    """The Lean realization of one accepted obligation.

    ``source`` is the complete declaration as fed to the checker. ``proof_body``
    is the proof part on its own, kept so an alignment link can point at the
    proof text without re-parsing. ``obligation_id`` and ``obligation_version``
    pin the artifact to a specific obligation version so a stale artifact can
    never silently attach to a revised obligation. ``kind`` tells the assembler
    whether ``source`` is a hole-filling snippet (root) or a standalone
    declaration (helper); it defaults to :attr:`ArtifactKind.PROOF_BODY` so the
    single-root assembly path is byte-for-byte unchanged.
    """

    source: str
    obligation_id: str
    obligation_version: int
    declaration_id: str | None = None
    source_span: tuple[int, int] | None = None
    proof_body: str = ""
    kind: ArtifactKind = ArtifactKind.PROOF_BODY

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "obligation_id": self.obligation_id,
            "obligation_version": self.obligation_version,
            "declaration_id": self.declaration_id,
            "source_span": list(self.source_span) if self.source_span else None,
            "proof_body": self.proof_body,
            "kind": self.kind.value,
        }


def lean_artifact_from_dict(data: dict[str, Any]) -> LeanArtifact:
    span = data.get("source_span")
    kind = data.get("kind", ArtifactKind.PROOF_BODY.value)
    return LeanArtifact(
        source=data["source"],
        obligation_id=data["obligation_id"],
        obligation_version=int(data["obligation_version"]),
        declaration_id=data.get("declaration_id"),
        source_span=tuple(span) if span else None,
        proof_body=data.get("proof_body", ""),
        kind=ArtifactKind(kind),
    )
