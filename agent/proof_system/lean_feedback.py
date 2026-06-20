"""Parse Lean checker output into structured diagnostic feedback."""

from __future__ import annotations

import hashlib
import re

from .base import DiagnosticCategory, GoalState, ParsedFeedback


_WHITESPACE_RE = re.compile(r"\s+", re.MULTILINE)
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


def _normalize_goal_text(goal: str) -> str:
    if not goal:
        return ""
    return _WHITESPACE_RE.sub(" ", goal.strip())


def _goal_fingerprint(goal: str) -> str:
    """Stable short id for one goal.

    Mirrors :func:`agent.search.metrics.goal_fingerprint` so the structured
    goal state and the Phase 0 baseline fingerprints agree on identity. Kept
    local to avoid a ``proof_system`` -> ``search`` dependency cycle.
    """
    normalized = _normalize_goal_text(goal)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def extract_goal_states(
    raw_output: str,
    *,
    declaration_id: str | None = None,
    source_span: tuple[int, int] | None = None,
) -> tuple[GoalState, ...]:
    """Lift raw goal blocks into structured, finger-printed goal states.

    A goal is flagged ``is_sorry_goal`` when its text still references
    ``sorry``/``admit`` — the proof is not actually closed even though the
    checker may have reported the declaration as accepted. ``source_span``
    is attached from the primary diagnostic location when known; multi-goal
    outputs share the same span for now.
    """
    states: list[GoalState] = []
    for block in extract_goal_blocks(raw_output):
        lowered = block.lower()
        states.append(
            GoalState(
                text=block,
                goal_fingerprint=_goal_fingerprint(block),
                declaration_id=declaration_id,
                source_span=source_span,
                is_sorry_goal=("sorry" in lowered or "admit" in lowered),
            )
        )
    return tuple(states)


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

        goal_blocks = extract_goal_blocks(raw_output)
        source_span = (line, column) if line is not None and column is not None else None
        return ParsedFeedback(
            category=category,
            message=first_meaningful_line(primary_output),
            line=line,
            column=column,
            unsolved_goals=goal_blocks,
            goal_state=extract_goal_states(raw_output, source_span=source_span),
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
