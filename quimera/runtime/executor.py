from __future__ import annotations

from .approval import ApprovalHandler
from .config import ToolRuntimeConfig
from .models import ToolCall, ToolResult
from .parser import ToolCallParseError, extract_tool_call
from .policy import ToolPolicy, ToolPolicyError, PathPermissionError
from .registry import ToolRegistry
from .tools.files import FileTools
from .tools.patch import PatchTool
from .tools.shell import ShellTool
from .tools.tasks import TaskTools


class ToolExecutor:
    """Executa um loop simples de tool calling com validação e aprovação."""

    def __init__(
        self,
        config: ToolRuntimeConfig,
        approval_handler: ApprovalHandler,
        registry: ToolRegistry | None = None,
        policy: ToolPolicy | None = None,
    ) -> None:
        self.config = config
        self.approval_handler = approval_handler
        self.registry = registry or ToolRegistry()
        self.policy = policy or ToolPolicy(config)
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        file_tools = FileTools(self.config)
        patch_tool = PatchTool(self.config)
        shell_tool = ShellTool(self.config)
        task_tools = TaskTools(self.config)
        self.registry.register("list_files", file_tools.list_files)
        self.registry.register("read_file", file_tools.read_file)
        self.registry.register("write_file", file_tools.write_file)
        self.registry.register("apply_patch", patch_tool.apply_patch)
        self.registry.register("grep_search", file_tools.grep_search)
        self.registry.register("run_shell", shell_tool.run_shell)
        # Task-related read-only tools
        self.registry.register("list_tasks", task_tools.list_tasks)
        self.registry.register("list_jobs", task_tools.list_jobs)
        self.registry.register("get_job", task_tools.get_job)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            self.policy.validate(call)
            
            permission_error = self.policy.check_path_permission(call)
            if permission_error:
                approved = self.approval_handler.approve(
                    tool_name=call.name,
                    summary=f"Permissão necessária para acessar: {permission_error.resolved_path}",
                )
                if not approved:
                    return ToolResult(ok=False, tool_name=call.name, error="Acesso negado pelo usuário")
            
            if self.policy.requires_approval(call):
                approved = self.approval_handler.approve(
                    tool_name=call.name,
                    summary=str(call.arguments),
                )
                if not approved:
                    return ToolResult(ok=False, tool_name=call.name, error="Execução negada pelo usuário")
            handler = self.registry.get(call.name)
            return handler(call)
        except ToolPolicyError as exc:
            return ToolResult(ok=False, tool_name=call.name, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=f"Falha inesperada: {exc}")

    def maybe_execute_from_response(self, response: str | None) -> tuple[str | None, ToolResult | None]:
        try:
            call = extract_tool_call(response)
        except ToolCallParseError as exc:
            return response, ToolResult(ok=False, tool_name="parse", error=str(exc))
        if call is None:
            return response, None
        result = self.execute(call)
        return response, result
