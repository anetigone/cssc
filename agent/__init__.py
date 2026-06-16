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
from .runtime.workspace import AttemptWorkspace, EphemeralCheckWorkspace, MaterializedCandidate
from .runtime.trace_store import JsonlTraceStore, result_events

logging.getLogger(__name__).addHandler(logging.NullHandler())

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
    "CandidateLibraryGenerator",
    "CheckResult",
    "ControllerConfig",
    "ControllerResult",
    "DiagnosticCategory",
    "EncodedProofState",
    "EphemeralCheckWorkspace",
    "FeedbackRepairGenerator",
    "LeanAdapter",
    "LeanTaskBuilder",
    "LexicalLeanRetriever",
    "load_dotenv",
    "JsonlTraceStore",
    "MaterializedCandidate",
    "ModelAdapterError",
    "OpenAIChatActionGenerator",
    "OpenAIChatConfig",
    "ParsedFeedback",
    "ProofSnippet",
    "ProgressSignal",
    "ProofController",
    "ProofSystemAdapter",
    "ProofTask",
    "RetrievalResult",
    "result_events",
    "StaticActionGenerator",
    "TaskBuildError",
    "TaskBuilderConfig",
    "UrllibChatTransport",
    "encode_proof_state",
]
