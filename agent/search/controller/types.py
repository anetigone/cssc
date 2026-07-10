"""Data types and protocol for the proof-search controller."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..budget import BudgetConfig, BudgetSnapshot
from ..execution import ExecutionMode
from ..memory import ProofMemory, empty_memory
from ..metrics import AttemptMetric, RunMetrics, new_sample_id
from ...proof_system.base import CandidateEdit, CheckResult, DiagnosticCategory

if TYPE_CHECKING:
    from ...proof_system.base import ParsedFeedback
    from ...retrieval import RetrievalResult
    from ...tasks.types import ProofTask


logger = logging.getLogger(__name__)


class Retriever(Protocol):
    """Minimal retrieval boundary used by the controller."""

    def retrieve(
        self,
        query: str | None = None,
        *,
        task: ProofTask | None = None,
        feedback: ParsedFeedback | None = None,
        top_k: int = 5,
    ) -> tuple[RetrievalResult, ...]:
        """Return snippets relevant to the current proof state."""
        ...


@dataclass(frozen=True)
class ControllerConfig:
    """Small policy knobs for the MVP controller."""

    max_candidates_per_model_call: int = 1
    candidate_extension: str = ".lean"
    stop_on_tool_unavailable: bool = True
    max_feedback_history: int = 5
    max_retrieval_results: int = 5
    retrieve_before_first_model_call: bool = False
    retrieve_on_categories: tuple[DiagnosticCategory, ...] = (
        DiagnosticCategory.UNKNOWN_IDENTIFIER,
        DiagnosticCategory.INVALID_REFERENCE,
        DiagnosticCategory.UNSOLVED_GOALS,
    )
    # 执行模式：由启动参数决定，运行中不可变；factory 是唯一选择点。
    execution_mode: ExecutionMode = ExecutionMode.MINIMAL
    # Structured frontier 排序策略（Phase 8.2）。用字符串而非枚举，避免共享
    # types 模块 import structured frontier；minimal 忽略此字段，structured
    # 在启动时校验值是否在 FrontierPolicy 范围内，未知值硬失败。
    frontier_policy: str = "legacy"


@dataclass(frozen=True)
class AttemptRecord:
    """One generated candidate and its checker result."""

    attempt_index: int
    candidate_id: str
    edit: CandidateEdit
    candidate_file: Path
    check_result: CheckResult


@dataclass(frozen=True)
class ControllerResult:
    """Final outcome of one controller run."""

    task: ProofTask
    accepted: bool
    attempts: tuple[AttemptRecord, ...]
    budget: BudgetSnapshot
    stop_reason: str
    accepted_attempt: AttemptRecord | None = None
    metrics: RunMetrics | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ControllerRunState:
    """Mutable working state for a single controller run.

    Encapsulates everything that changes from one loop iteration to the next so
    that ``run()`` can be expressed as a short pipeline of phase methods.
    """

    attempts: list[AttemptRecord] = field(default_factory=list)
    feedback_history: list[ParsedFeedback] = field(default_factory=list)
    retrieved_history: list[RetrievalResult] = field(default_factory=list)
    seen_candidate_keys: set[tuple[str, str]] = field(default_factory=set)
    current_retrieved: tuple[RetrievalResult, ...] = ()
    stop_reason: str = "budget"
    attempt_index: int = 0
    retrieved_this_iteration: bool = False
    attempt_metrics: list[AttemptMetric] = field(default_factory=list)
    # Self-managed compact memory updated after every check; replaces the fixed
    # full-history stack as the loop's carried context.
    memory: ProofMemory = field(default_factory=empty_memory)
    # Accepted candidates that the safety reviewer rejected, with reasons.
    safety_rejections: list[dict[str, Any]] = field(default_factory=list)
    # One normalized usage record per model-generation call. Hidden reasoning
    # remains diagnostic data and is excluded from run-level output tokens.
    model_usage: list[dict[str, int]] = field(default_factory=list)
    generation_failures: list[dict[str, Any]] = field(default_factory=list)
    # Unique per controller run so trace events from repeated runs never collide.
    sample_id: str = field(default_factory=new_sample_id)
