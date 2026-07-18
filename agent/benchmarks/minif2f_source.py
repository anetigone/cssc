"""Source-layout validation and theorem extraction for miniF2F."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


EXPECTED_SPLIT_COUNTS = {"valid": 244, "test": 244}
SOURCE_FILES = {"valid": "Valid.lean", "test": "Test.lean"}
SOURCE_URL = "https://github.com/google-deepmind/miniF2F.git"
HOLE_MARKER = "{{proof}}"

_THEOREM_RE = re.compile(
    r"(?m)^theorem[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)"
)
_PROOF_START_RE = re.compile(r":=\s*(?:by\b|rfl\b)")
_NEXT_DOC_COMMENT_RE = re.compile(r"(?m)^/--")
_PREAMBLE_RE = re.compile(r"(?m)^(?:import\b[^\r\n]*|open\b[^\r\n]*)$")


class MiniF2FError(ValueError):
    """Raised when a miniF2F source or prepared suite violates its contract."""


@dataclass(frozen=True)
class MiniF2FDeclaration:
    """One canonical upstream theorem and its generated scaffold."""

    task_id: str
    split: str
    source_file: str
    source_line: int
    statement: str
    scaffold: str

    @property
    def statement_sha256(self) -> str:
        return sha256_text(self.statement)

    @property
    def scaffold_sha256(self) -> str:
        return sha256_text(self.scaffold)


def resolve_source_root(
    source_root: str | Path | None = None,
    *,
    repository_root: str | Path = ".",
) -> Path:
    candidate = (
        Path(source_root)
        if source_root is not None
        else Path("benchmark/miniF2F")
    )
    if not candidate.is_absolute():
        candidate = Path(repository_root) / candidate
    resolved = candidate.resolve()
    validate_source_layout(resolved)
    return resolved


def extract_source_declarations(
    source_root: str | Path,
    *,
    expected_split_counts: dict[str, int] | None = None,
) -> tuple[MiniF2FDeclaration, ...]:
    root = Path(source_root).resolve()
    validate_source_layout(root)
    expected = expected_split_counts or EXPECTED_SPLIT_COUNTS
    declarations: list[MiniF2FDeclaration] = []
    for split, filename in SOURCE_FILES.items():
        path = root / "MiniF2F" / filename
        source = normalize_newlines(path.read_text(encoding="utf-8"))
        split_declarations = extract_split(
            source,
            split=split,
            source_file=f"MiniF2F/{filename}",
        )
        wanted = expected.get(split)
        if wanted is not None and len(split_declarations) != wanted:
            raise MiniF2FError(
                f"{filename} produced {len(split_declarations)} canonical tasks; "
                f"expected {wanted}. The upstream revision or parser contract "
                "may have changed."
            )
        declarations.extend(split_declarations)

    names_by_split = {
        split: {item.task_id for item in declarations if item.split == split}
        for split in SOURCE_FILES
    }
    overlap = names_by_split["valid"] & names_by_split["test"]
    if overlap:
        sample = ", ".join(sorted(overlap)[:5])
        raise MiniF2FError(f"task ids occur in both valid and test: {sample}")
    if len(declarations) != len({item.task_id for item in declarations}):
        raise MiniF2FError("duplicate canonical task ids found")
    return tuple(declarations)


def extract_split(
    source: str,
    *,
    split: str,
    source_file: str,
) -> list[MiniF2FDeclaration]:
    matches = list(_THEOREM_RE.finditer(source))
    if not matches:
        raise MiniF2FError(
            f"{source_file}: no top-level theorem declarations found"
        )
    preamble = "\n".join(
        match.group(0)
        for match in _PREAMBLE_RE.finditer(source[: matches[0].start()])
    )
    if "import MiniF2F.ProblemImports" not in preamble:
        raise MiniF2FError(f"{source_file}: missing MiniF2F.ProblemImports")
    declarations: list[MiniF2FDeclaration] = []
    for index, match in enumerate(matches):
        name = match.group("name")
        next_start = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(source)
        )
        chunk = source[match.start() : next_start]
        next_doc = _NEXT_DOC_COMMENT_RE.search(
            chunk, match.end() - match.start()
        )
        declaration = chunk[: next_doc.start() if next_doc else len(chunk)].rstrip()
        proof_starts = list(_PROOF_START_RE.finditer(declaration))
        if not proof_starts:
            raise MiniF2FError(
                f"{source_file}:{name}: could not identify theorem proof boundary"
            )
        if ".variants." in name:
            continue
        # A chunk may contain a commented experimental theorem before the next
        # docstring. The first proof boundary belongs to the active theorem.
        statement = declaration[: proof_starts[0].start()].rstrip() + "\n"
        scaffold_declaration = (
            statement.rstrip() + f" := by\n  {HOLE_MARKER}\n"
        )
        scaffold = f"{preamble}\n\n{scaffold_declaration}"
        declarations.append(
            MiniF2FDeclaration(
                task_id=name,
                split=split,
                source_file=source_file,
                source_line=source.count("\n", 0, match.start()) + 1,
                statement=statement,
                scaffold=scaffold,
            )
        )
    return declarations


def validate_source_layout(root: Path) -> None:
    required = [
        root / "MiniF2F" / "Valid.lean",
        root / "MiniF2F" / "Test.lean",
        root / "MiniF2F" / "ProblemImports.lean",
        root / "lean-toolchain",
        root / "lakefile.lean",
        root / "lake-manifest.json",
        root / "LICENSE",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise MiniF2FError(
            "invalid miniF2F checkout; missing: " + ", ".join(missing)
        )


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def sha256_text(value: str) -> str:
    return sha256_bytes(normalize_newlines(value).encode("utf-8"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
