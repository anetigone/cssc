"""Agent role implementations and shared model infrastructure."""

from .chat_driver import ChatDriver, first_choice_message
from .config import AgentRole, RoleModelConfig
from .formalization import (
    ChatFormalizationAgent,
    FormalizationAgent,
    FormalizationRequest,
    FormalizationResult,
    OpenAIChatFormalizationAgent,
    ScaffoldChecker,
    StaticFormalizationAgent,
    VerifiedFormalizationCache,
)
from .openai import (
    ChatConfig,
    ChatTransport,
    ModelAdapterError,
    OpenAIChatConfig,
    UrllibChatTransport,
)
from .proof import ChatActionGenerator, OpenAIChatActionGenerator
from .tools import (
    FunctionTool,
    LeanEnvironmentToolProvider,
    LeanProofToolProvider,
    Tool,
    ToolCall,
    ToolResult,
    extract_missing_imports,
    extract_tool_calls,
)

__all__ = [
    "AgentRole",
    "ChatActionGenerator",
    "ChatConfig",
    "ChatDriver",
    "ChatFormalizationAgent",
    "ChatTransport",
    "FormalizationAgent",
    "FormalizationRequest",
    "FormalizationResult",
    "FunctionTool",
    "LeanEnvironmentToolProvider",
    "LeanProofToolProvider",
    "ModelAdapterError",
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
    "first_choice_message",
]
