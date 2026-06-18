"""Core agent package for cost-sensitive proof search."""

import logging

from .search.action import ActionCandidate, ActionGenerationRequest, ActionGenerator, StaticActionGenerator
from .search.budget import BudgetConfig, BudgetExhausted, BudgetManager, BudgetSnapshot
from .search.controller import AttemptRecord, ControllerConfig, ControllerResult, ProofController
from .search.proposer import CandidateLibraryGenerator, ProofSnippet
from .search.repair import FeedbackRepairGenerator
from .search.state_encoder import EncodedProofState, encode_proof_state
from .runtime.env_loader import load_dotenv
from .proof_system.lean import LeanAdapter
from .retrieval import LexicalLeanRetriever, RetrievalResult
from .agents import (
    AgentRole,
    ChatTransport,
    FormalizationAgent,
    FormalizationRequest,
    FormalizationResult,
    FunctionTool,
    LeanEnvironmentToolProvider,
    ModelAdapterError,
    OpenAIChatActionGenerator,
    OpenAIChatConfig,
    OpenAIChatFormalizationAgent,
    RoleModelConfig,
    ScaffoldChecker,
    StaticFormalizationAgent,
    Tool,
    ToolCall,
    ToolResult,
    UrllibChatTransport,
    VerifiedFormalizationCache,
    extract_missing_imports,
    extract_tool_calls,
)
from .input import (
    InputNormalizer,
    LeanAdapterScaffoldChecker,
    NormalizedInput,
    ScaffoldValidationError,
    ValidationConfig,
    prepare_tasks,
    validate_scaffold_json,
)
from .proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
)
from .tasks.task_builder import LeanTaskBuilder, TaskBuildError, TaskBuilderConfig
from .tasks.types import ProofTask, TaskInputKind, TaskInputSpec
from .runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace, MaterializedCandidate
from .runtime.trace_store import JsonlTraceStore, result_events

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ActionCandidate",
    "ActionGenerationRequest",
    "ActionGenerator",
    "AgentRole",
    "AttemptRecord",
    "AttemptWorkspace",
    "BudgetConfig",
    "BudgetExhausted",
    "BudgetManager",
    "BudgetSlice",
    "BudgetSnapshot",
    "CandidateEdit",
    "CandidateLibraryGenerator",
    "CheckResult",
    "ControllerConfig",
    "ControllerResult",
    "DiagnosticCategory",
    "EncodedProofState",
    "EphemeralCheckWorkspace",
    "FeedbackRepairGenerator",
    "FormalizationAgent",
    "FormalizationRequest",
    "FormalizationResult",
    "FunctionTool",
    "InputNormalizer",
    "LeanAdapter",
    "LeanAdapterScaffoldChecker",
    "LeanEnvironmentToolProvider",
    "LeanTaskBuilder",
    "LexicalLeanRetriever",
    "load_dotenv",
    "JsonlTraceStore",
    "MaterializedCandidate",
    "ModelAdapterError",
    "NormalizedInput",
    "prepare_tasks",
    "ScaffoldChecker",
    "ScaffoldValidationError",
    "Tool",
    "ToolCall",
    "ToolResult",
    "ValidationConfig",
    "validate_scaffold_json",
    "OpenAIChatActionGenerator",
    "OpenAIChatConfig",
    "OpenAIChatFormalizationAgent",
    "ParsedFeedback",
    "ProofSnippet",
    "ProgressSignal",
    "ProofController",
    "ProofSystemAdapter",
    "ProofTask",
    "RetrievalResult",
    "RoleModelConfig",
    "result_events",
    "StaticActionGenerator",
    "StaticFormalizationAgent",
    "TaskBuildError",
    "TaskBuilderConfig",
    "TaskInputKind",
    "TaskInputSpec",
    "UrllibChatTransport",
    "VerifiedFormalizationCache",
    "encode_proof_state",
    "extract_missing_imports",
    "extract_tool_calls",
]
