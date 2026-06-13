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
from .process_supervisor import ManagedProcess, ProcessSupervisor
from .registry import ToolRegistry
from .task_executor import TaskExecutor, create_executor
from .task_runner import TaskRunner
from .task_reviewer import TaskReviewer

__all__ = [
    "ApprovalHandler",
    "AutoApprovalHandler",
    "ConsoleApprovalHandler",
    "NonBlockingConsoleApprovalHandler",
    "PreApprovalHandler",
    "ManagedProcess",
    "ProcessSupervisor",
    "ToolRuntimeConfig",
    "ToolExecutor",
    "ToolCall",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
    "TaskExecutor",
    "create_executor",
    "TaskRunner",
    "TaskReviewer",
]
