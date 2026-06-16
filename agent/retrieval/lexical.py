"""Lightweight lexical retrieval over local Lean declarations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..proof_system.base import ParsedFeedback, ProofTask


_DECL_START_RE = re.compile(r"^\s*(theorem|lemma|def|example)\s+([A-Za-z_][\w'.]*)\b")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")


@dataclass(frozen=True)
class RetrievalResult:
    """One retrieved Lean declaration or source snippet."""

    name: str
    source_path: str | None
    start_line: int
    snippet: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Document:
    name: str
    source_path: str | None
    start_line: int
    snippet: str
    tokens: frozenset[str]


class LexicalLeanRetriever:
    """Rank local Lean declarations by token overlap with a task or query."""

    def __init__(self, documents: Sequence[_Document]) -> None:
        self._documents = tuple(documents)

    @classmethod
    def from_paths(cls, paths: Iterable[str | Path]) -> "LexicalLeanRetriever":
        documents: list[_Document] = []
        for path in paths:
            source_path = Path(path)
            if source_path.is_dir():
                for lean_file in sorted(source_path.rglob("*.lean")):
                    documents.extend(_documents_from_source(lean_file.read_text(encoding="utf-8"), str(lean_file)))
            else:
                documents.extend(_documents_from_source(source_path.read_text(encoding="utf-8"), str(source_path)))
        return cls(documents)

    @classmethod
    def from_sources(cls, sources: dict[str, str]) -> "LexicalLeanRetriever":
        documents: list[_Document] = []
        for source_name, source in sources.items():
            documents.extend(_documents_from_source(source, source_name))
        return cls(documents)

    def retrieve(
        self,
        query: str | None = None,
        *,
        task: ProofTask | None = None,
        feedback: ParsedFeedback | None = None,
        top_k: int = 5,
    ) -> tuple[RetrievalResult, ...]:
        query_text = _query_text(query, task, feedback)
        query_tokens = _tokens(query_text)
        if not query_tokens:
            return ()

        scored: list[RetrievalResult] = []
        for document in self._documents:
            overlap = query_tokens & document.tokens
            if not overlap:
                continue
            score = len(overlap) / max(len(query_tokens), 1)
            scored.append(
                RetrievalResult(
                    name=document.name,
                    source_path=document.source_path,
                    start_line=document.start_line,
                    snippet=document.snippet,
                    score=score,
                    metadata={"matched_tokens": tuple(sorted(overlap))},
                )
            )
        scored.sort(key=lambda item: (-item.score, item.name, item.source_path or ""))
        return tuple(scored[:top_k])


def _documents_from_source(source: str, source_path: str | None) -> list[_Document]:
    lines = source.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _DECL_START_RE.match(line)
        if match:
            starts.append((index, match.group(2)))

    documents: list[_Document] = []
    for position, (start, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        snippet = "\n".join(lines[start:end]).strip()
        documents.append(
            _Document(
                name=name,
                source_path=source_path,
                start_line=start + 1,
                snippet=snippet,
                tokens=frozenset(_tokens(snippet)),
            )
        )
    return documents


def _query_text(
    query: str | None,
    task: ProofTask | None,
    feedback: ParsedFeedback | None,
) -> str:
    parts = [query or ""]
    if task is not None:
        parts.append(task.source_template)
        parts.extend(str(value) for value in task.metadata.values() if isinstance(value, str))
    if feedback is not None:
        parts.append(feedback.message)
        parts.extend(feedback.unsolved_goals)
    return "\n".join(part for part in parts if part)


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(text):
        lowered = token.lower()
        if len(lowered) > 1:
            tokens.add(lowered)
        for part in re.split(r"[_.']", lowered):
            if len(part) > 1:
                tokens.add(part)
    return tokens
