"""Proof-system-neutral interfaces and data structures.

The controller should depend on this module rather than on Lean-specific
execution details. The first concrete backend is Lean, but these types keep the
boundary small enough to extend later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..tasks.types import ProofTask


class DiagnosticCategory(str, Enum):
    """Normalized verifier outcomes used by the search controller."""

    PROOF_ACCEPTED = "proof_accepted"
    PARSER_ERROR = "parser_error"
    UNKNOWN_IDENTIFIER = "unknown_identifier"
    TYPE_MISMATCH = "type_mismatch"
    UNSOLVED_GOALS = "unsolved_goals"
    TACTIC_FAILED = "tactic_failed"
    TERMINATION_ISSUE = "termination_or_recursion_issue"
    INVALID_REFERENCE = "invalid_reference"
    TIMEOUT = "timeout"
    TOOL_UNAVAILABLE = "tool_unavailable"
    CHECKER_ERROR = "checker_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CandidateEdit:
    """A candidate replacement for the task's proof hole."""

    text: str
    action: str = "manual"
    parent_node_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetSlice:
    """Small verifier budget allocation for one check."""

    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ParsedFeedback:
    """Structured diagnostic information extracted from verifier output."""

    category: DiagnosticCategory
    message: str = ""
    line: int | None = None
    column: int | None = None
    unsolved_goals: tuple[str, ...] = ()
    raw_output: str = ""


@dataclass(frozen=True)
class ProgressSignal:
    """Feature vector for comparing a child proof state with its parent."""

    accepted_prefix_chars: int = 0
    goal_count_delta: int | None = None
    goal_size_delta: int | None = None
    diagnostic_category: DiagnosticCategory = DiagnosticCategory.UNKNOWN
    introduced_obligations: bool = False
    moved_to_semantic_obligation: bool = False
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    """Result of running a proof checker on one materialized candidate."""

    accepted: bool
    category: DiagnosticCategory
    raw_output: str
    candidate_file: Path | None = None
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    elapsed_seconds: float = 0.0
    parsed_feedback: ParsedFeedback | None = None
    progress: ProgressSignal | None = None


class ProofSystemAdapter(ABC):
    """Minimal boundary between search control and a concrete prover."""

    @abstractmethod
    def render_candidate(self, task: ProofTask, candidate_edit: CandidateEdit) -> str:
        """Render a complete source file from a task template and candidate."""

    @abstractmethod
    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        """Run the proof checker on a materialized candidate file."""

    @abstractmethod
    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        """Normalize raw checker output into a diagnostic category."""

    @abstractmethod
    def extract_progress(
        self,
        parent_state: Any,
        check_result: CheckResult,
    ) -> ProgressSignal:
        """Compute lightweight progress features from a check result."""
