"""Core agent package for cost-sensitive proof search."""

from .search.action import ActionCandidate, ActionGenerationRequest, ActionGenerator, StaticActionGenerator
from .search.budget import BudgetConfig, BudgetExhausted, BudgetManager, BudgetSnapshot
from .search.controller import AttemptRecord, ControllerConfig, ControllerResult, ProofController
from .runtime.env_loader import load_dotenv
from .proof_system.lean import LeanAdapter
from .model.openai_chat import (
    ModelAdapterError,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
    UrllibChatTransport,
)
from .proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from .tasks.task_builder import LeanTaskBuilder, TaskBuildError, TaskBuilderConfig
from .runtime.workspace import AttemptWorkspace, MaterializedCandidate
from .runtime.trace_store import JsonlTraceStore, result_events

__all__ = [
    "ActionCandidate",
    "ActionGenerationRequest",
    "ActionGenerator",
    "AttemptRecord",
    "AttemptWorkspace",
    "BudgetConfig",
    "BudgetExhausted",
    "BudgetManager",
    "BudgetSlice",
    "BudgetSnapshot",
    "CandidateEdit",
    "CheckResult",
    "ControllerConfig",
    "ControllerResult",
    "DiagnosticCategory",
    "LeanAdapter",
    "LeanTaskBuilder",
    "load_dotenv",
    "JsonlTraceStore",
    "MaterializedCandidate",
    "ModelAdapterError",
    "OpenAIChatActionGenerator",
    "OpenAIChatConfig",
    "ParsedFeedback",
    "ProgressSignal",
    "ProofController",
    "ProofSystemAdapter",
    "ProofTask",
    "result_events",
    "StaticActionGenerator",
    "TaskBuildError",
    "TaskBuilderConfig",
    "UrllibChatTransport",
]
