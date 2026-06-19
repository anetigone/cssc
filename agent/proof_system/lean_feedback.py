"""Parse Lean checker output into structured diagnostic feedback."""

from __future__ import annotations

import re

from .base import DiagnosticCategory, ParsedFeedback


_LOCATION_RE = re.compile(
    r":(?P<line>\d+):(?P<column>\d+):\s+(?:error(?:\([^)]*\))?|warning):",
    re.IGNORECASE,
)
_DIAGNOSTIC_LINE_RE = re.compile(
    r"^.*?:\d+:\d+:\s+(?:error(?:\([^)]*\))?|warning|information|hint):",
    re.IGNORECASE,
)


def contains_sorry_warning(raw_output: str) -> bool:
    normalized = raw_output.lower()
    return bool(
        re.search(r"\bdeclaration\b.*\buses\b.*\bsorry\b", normalized)
        or re.search(r"\bwarning\b.*\bsorry\b", normalized)
        or re.search(r"\bsorry\b.*\baxiom\b", normalized)
    )


def contains_error_diagnostic(raw_output: str) -> bool:
    return bool(re.search(r"\berror(?:\([^)]*\))?:", raw_output, re.IGNORECASE))


def first_location(raw_output: str) -> tuple[int | None, int | None]:
    match = _LOCATION_RE.search(raw_output)
    if not match:
        return None, None
    return int(match.group("line")), int(match.group("column"))


def first_meaningful_line(raw_output: str) -> str:
    for line in raw_output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def primary_error_block(raw_output: str) -> str:
    """Return the first fatal diagnostic and its continuation lines."""
    lines = raw_output.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r":\s+error(?:\([^)]*\))?:", line, re.IGNORECASE):
            continue
        block = [line]
        for continuation in lines[index + 1 :]:
            if _DIAGNOSTIC_LINE_RE.match(continuation):
                break
            block.append(continuation)
        return "\n".join(block).strip()
    return ""


def extract_goal_blocks(raw_output: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    capture = False
    for line in raw_output.splitlines():
        if "unsolved goals" in line.lower():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            capture = True
            continue
        if capture:
            if line.strip().startswith("error:") and current:
                blocks.append("\n".join(current).strip())
                current = []
                capture = False
            else:
                current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return tuple(block for block in blocks if block)


class LeanFeedbackParser:
    """Normalize raw Lean checker output into structured feedback."""

    def parse(self, raw_output: str) -> ParsedFeedback:
        primary_output = primary_error_block(raw_output) or raw_output
        normalized = primary_output.lower()
        line, column = first_location(primary_output)

        if not raw_output.strip():
            return ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED,
                message="Proof accepted.",
                raw_output=raw_output,
            )

        category = self._classify(normalized, raw_output)

        return ParsedFeedback(
            category=category,
            message=first_meaningful_line(primary_output),
            line=line,
            column=column,
            unsolved_goals=extract_goal_blocks(raw_output),
            raw_output=raw_output,
        )

    def _classify(
        self, normalized_primary_output: str, raw_output: str
    ) -> DiagnosticCategory:
        normalized = normalized_primary_output
        if (
            "no default toolchain configured" in normalized
            or "toolchain" in normalized
            and "not installed" in normalized
        ):
            return DiagnosticCategory.TOOL_UNAVAILABLE
        if "unknown identifier" in normalized or "unknown constant" in normalized:
            return DiagnosticCategory.UNKNOWN_IDENTIFIER
        if "type mismatch" in normalized or "application type mismatch" in normalized:
            return DiagnosticCategory.TYPE_MISMATCH
        if "unsolved goals" in normalized or "goals unsolved" in normalized:
            return DiagnosticCategory.UNSOLVED_GOALS
        if "tactic" in normalized and (
            "failed" in normalized or "unsolved" in normalized
        ):
            return DiagnosticCategory.TACTIC_FAILED
        if "failed to synthesize" in normalized:
            return DiagnosticCategory.TYPE_MISMATCH
        if "unexpected token" in normalized or "parser" in normalized:
            return DiagnosticCategory.PARSER_ERROR
        if "termination" in normalized or "failed to prove termination" in normalized:
            return DiagnosticCategory.TERMINATION_ISSUE
        if "invalid" in normalized and (
            "theorem" in normalized or "declaration" in normalized
        ):
            return DiagnosticCategory.INVALID_REFERENCE
        if "error:" in normalized:
            return DiagnosticCategory.CHECKER_ERROR
        if contains_sorry_warning(raw_output):
            return DiagnosticCategory.UNSOLVED_GOALS
        return DiagnosticCategory.UNKNOWN
