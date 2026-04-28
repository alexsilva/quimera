"""Runtime seguro de ferramentas para agentes locais."""

from .approval import (
    ApprovalHandler,
    AutoApprovalHandler,
    ConsoleApprovalHandler,
    NonBlockingConsoleApprovalHandler,
    PreApprovalHandler,
)
from .config import ToolRuntimeConfig
from .executor import ToolExecutor
from .models import ToolCall, ToolResult
from .policy import ToolPolicy
from .registry import ToolRegistry
from .task_executor import TaskExecutor, create_executor

__all__ = [
    "ApprovalHandler",
    "AutoApprovalHandler",
    "ConsoleApprovalHandler",
    "NonBlockingConsoleApprovalHandler",
    "PreApprovalHandler",
    "ToolRuntimeConfig",
    "ToolExecutor",
    "ToolCall",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
    "TaskExecutor",
    "create_executor",
]
