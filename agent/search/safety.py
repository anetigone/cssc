"""Post-check safety review for accepted proofs.

Phase 1 adds a deterministic :class:`StatementSafetyReviewer` that runs only
after the Lean checker has accepted a candidate. It guards against proof
shortcuts the checker cannot always catch on its own:

* the original theorem/lemma statement being silently rewritten;
* residual ``sorry`` / ``admit`` tactics;
* a newly introduced ``axiom`` declaration.

A model-driven reviewer is deliberately out of scope here: known cheating
patterns are checked mechanically, and the model reviewer only exists as a
Protocol placeholder for a later phase.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from ..proof_system.base import CheckResult, ProofTask


logger = logging.getLogger(__name__)


# Residual shortcuts that close a goal without proving it.
_SHORTCUT_RE = re.compile(r"\b(sorry|admit)\b")

# Top-level ``axiom`` declarations introduce unproven assumptions. Capture the
# declaration name so an existing axiom does not mask a different one added by
# the candidate.
_AXIOM_RE = re.compile(r"^[ \t]*axiom[ \t]+([^\s:({]+)", re.MULTILINE)

# A bare ``sorry``/``admit`` term on its own also bypasses the obligation.
# Matched independently so the reason names the precise shortcut.
_ADMIT_TERM_RE = re.compile(r":=\s*(sorry|admit)\b")


@dataclass(frozen=True)
class SafetyVerdict:
    """Outcome of a safety review of an accepted candidate."""

    accepted: bool
    reasons: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


class SafetyReviewer(Protocol):
    """Boundary the controller uses to vet accepted proofs.

    The reviewer never generates a proof and never attributes mathematical
    errors; it only checks the accepted artifact for known cheating patterns.
    """

    def accepts(
        self,
        task: ProofTask,
        candidate_source: str,
        check_result: CheckResult,
    ) -> SafetyVerdict:
        """Return a verdict for an already checker-accepted candidate."""
        ...


class StatementSafetyReviewer:
    """Deterministic statement-preservation and anti-cheating reviewer."""

    def accepts(
        self,
        task: ProofTask,
        candidate_source: str,
        check_result: CheckResult,
    ) -> SafetyVerdict:
        del check_result  # Accepted status is implied by the caller.

        reasons: list[str] = []

        statement_reason = self._statement_preservation_reason(task, candidate_source)
        if statement_reason is not None:
            reasons.append(statement_reason)

        scanned_candidate = _strip_lean_comments_and_strings(candidate_source)
        reasons.extend(self._shortcut_reasons(scanned_candidate))
        reasons.extend(self._axiom_reasons(task, scanned_candidate))

        verdict = SafetyVerdict(
            accepted=not reasons,
            reasons=tuple(reasons),
        )
        if reasons:
            logger.warning(
                "Safety review rejected accepted candidate: task_id=%s reasons=%s",
                task.task_id,
                verdict.reasons,
            )
        else:
            logger.debug("Safety review accepted candidate: task_id=%s", task.task_id)
        return verdict

    def _statement_preservation_reason(
        self,
        task: ProofTask,
        candidate_source: str,
    ) -> str | None:
        """Detect a rewritten statement header.

        The task template is split at the editable hole. Everything before the
        hole — imports, declaration keyword, theorem/lemma signature — must be
        preserved verbatim in the rendered candidate. Any drift there means the
        model changed what it was supposed to prove.
        """
        hole_index = task.source_template.find(task.hole_marker)
        if hole_index < 0:
            # No hole marker: the whole template is the fixed prefix.
            prefix = task.source_template
        else:
            prefix = task.source_template[:hole_index]
        prefix = prefix.rstrip()
        if not prefix:
            return None
        if not candidate_source.rstrip().startswith(prefix):
            return "statement_not_preserved"
        return None

    def _shortcut_reasons(self, candidate_source: str) -> list[str]:
        reasons: list[str] = []
        for match in _SHORTCUT_RE.finditer(candidate_source):
            reasons.append(f"residual_shortcut:{match.group(1)}")
            break  # One reason per shortcut kind is enough.
        if _ADMIT_TERM_RE.search(candidate_source) and not any(
            r.startswith("residual_shortcut:") for r in reasons
        ):
            reasons.append("residual_shortcut:admit")
        # Deduplicate while preserving order.
        return _dedupe(reasons)

    def _axiom_reasons(self, task: ProofTask, candidate_source: str) -> list[str]:
        existing_source = _strip_lean_comments_and_strings(task.source_template)
        existing = Counter(_AXIOM_RE.findall(existing_source))
        candidate = Counter(_AXIOM_RE.findall(candidate_source))
        if any(count > existing[name] for name, count in candidate.items()):
            return ["new_axiom_declared"]
        return []


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _strip_lean_comments_and_strings(source: str) -> str:
    """Blank comments and strings while preserving source line boundaries."""
    output: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if block_depth:
            if char == "/" and next_char == "-":
                block_depth += 1
                output.extend((" ", " "))
                index += 2
            elif char == "-" and next_char == "/":
                block_depth -= 1
                output.extend((" ", " "))
                index += 2
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue

        if in_string:
            if char == "\\" and next_char:
                output.extend((" ", " "))
                index += 2
            else:
                if char == '"':
                    in_string = False
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue

        if char == "-" and next_char == "-":
            while index < len(source) and source[index] != "\n":
                output.append(" ")
                index += 1
        elif char == "/" and next_char == "-":
            block_depth = 1
            output.extend((" ", " "))
            index += 2
        elif char == '"':
            in_string = True
            output.append(" ")
            index += 1
        else:
            output.append(char)
            index += 1
    return "".join(output)
