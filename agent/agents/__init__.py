"""Agent role implementations and shared model infrastructure."""

from .config import AgentRole, RoleModelConfig
from .formalization import (
    FormalizationAgent,
    FormalizationRequest,
    FormalizationResult,
    OpenAIChatFormalizationAgent,
    ScaffoldChecker,
    StaticFormalizationAgent,
    VerifiedFormalizationCache,
)
from .openai import ChatTransport, ModelAdapterError, OpenAIChatConfig, UrllibChatTransport
from .proof import OpenAIChatActionGenerator

__all__ = [
    "AgentRole",
    "ChatTransport",
    "FormalizationAgent",
    "FormalizationRequest",
    "FormalizationResult",
    "ModelAdapterError",
    "OpenAIChatActionGenerator",
    "OpenAIChatConfig",
    "OpenAIChatFormalizationAgent",
    "RoleModelConfig",
    "ScaffoldChecker",
    "StaticFormalizationAgent",
    "UrllibChatTransport",
    "VerifiedFormalizationCache",
]
