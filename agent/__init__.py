"""Core agent package for cost-sensitive proof search."""

from .action import ActionCandidate, ActionGenerationRequest, ActionGenerator, StaticActionGenerator
from .budget import BudgetConfig, BudgetExhausted, BudgetManager, BudgetSnapshot
from .controller import AttemptRecord, ControllerConfig, ControllerResult, ProofController
from .env_loader import load_dotenv
from .lean_adapter import LeanAdapter
from .model_adapter import (
    ModelAdapterError,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
    UrllibChatTransport,
)
from .proof_system_adapter import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from .task_builder import LeanTaskBuilder, TaskBuildError, TaskBuilderConfig
from .workspace import AttemptWorkspace, MaterializedCandidate

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
    "MaterializedCandidate",
    "ModelAdapterError",
    "OpenAIChatActionGenerator",
    "OpenAIChatConfig",
    "ParsedFeedback",
    "ProgressSignal",
    "ProofController",
    "ProofSystemAdapter",
    "ProofTask",
    "StaticActionGenerator",
    "TaskBuildError",
    "TaskBuilderConfig",
    "UrllibChatTransport",
]
