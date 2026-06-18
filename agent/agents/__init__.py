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
from .tools import (
    FunctionTool,
    LeanEnvironmentToolProvider,
    Tool,
    ToolCall,
    ToolResult,
    extract_missing_imports,
    extract_tool_calls,
)

__all__ = [
    "AgentRole",
    "ChatTransport",
    "FormalizationAgent",
    "FormalizationRequest",
    "FormalizationResult",
    "FunctionTool",
    "LeanEnvironmentToolProvider",
    "ModelAdapterError",
    "OpenAIChatActionGenerator",
    "OpenAIChatConfig",
    "OpenAIChatFormalizationAgent",
    "RoleModelConfig",
    "ScaffoldChecker",
    "StaticFormalizationAgent",
    "Tool",
    "ToolCall",
    "ToolResult",
    "UrllibChatTransport",
    "VerifiedFormalizationCache",
    "extract_missing_imports",
    "extract_tool_calls",
]
