"""Componentes de `quimera.runtime.executor`."""
from __future__ import annotations

from .approval import ApprovalHandler
from .config import ToolRuntimeConfig
from .models import ToolCall, ToolResult
from .parser import ToolCallParseError, extract_tool_call
from .policy import ToolPolicy, ToolPolicyError
from .registry import ToolRegistry
from .tools.files import FileTools
from .tools.patch import PatchTool
from .tools.shell import ShellTool
from .tools.tasks import TaskTools


class ToolExecutor:
    """Executa um loop simples de tool calling com validação e aprovação."""

    _ALIASES = {
        "run": "run_shell",
        "run_shell_command": "run_shell",
        "execute_command": "exec_command",
    }

    def __init__(
            self,
            config: ToolRuntimeConfig,
            approval_handler: ApprovalHandler,
            registry: ToolRegistry | None = None,
            policy: ToolPolicy | None = None,
    ) -> None:
        """Inicializa uma instância de ToolExecutor."""
        self.config = config
        self.approval_handler = approval_handler
        self.registry = registry or ToolRegistry()
        self.policy = policy or ToolPolicy(config)
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        """Executa register builtin tools."""
        file_tools = FileTools(self.config)
        patch_tool = PatchTool(self.config)
        shell_tool = ShellTool(self.config)
        task_tools = TaskTools(self.config)
        self.registry.register("list_files", file_tools.list_files)
        self.registry.register("read_file", file_tools.read_file)
        self.registry.register("write_file", file_tools.write_file)
        self.registry.register("remove_file", file_tools.remove_file)
        self.registry.register("apply_patch", patch_tool.apply_patch)
        self.registry.register("grep_search", file_tools.grep_search)
        self.registry.register("run_shell", shell_tool.run_shell)
        self.registry.register("run_shell_command", shell_tool.run_shell)
        self.registry.register("exec_command", shell_tool.exec_command)
        self.registry.register("write_stdin", shell_tool.write_stdin)
        self.registry.register("close_command_session", shell_tool.close_command_session)
        # Task-related read-only tools
        self.registry.register("list_tasks", task_tools.list_tasks)
        self.registry.register("list_jobs", task_tools.list_jobs)
        self.registry.register("get_job", task_tools.get_job)

    def execute(self, call: ToolCall) -> ToolResult:
        """Executa execute."""
        normalized_call = self._normalize_call(call)
        try:
            self.policy.validate(normalized_call)

            permission_error = self.policy.check_path_permission(normalized_call)
            if permission_error:
                approved = self.approval_handler.approve(
                    tool_name=normalized_call.name,
                    summary=f"Permissão necessária para acessar: {permission_error.resolved_path}",
                )
                if not approved:
                    return ToolResult(ok=False, tool_name=normalized_call.name, error="Acesso negado pelo usuário")

            if self.policy.requires_approval(normalized_call):
                approved = self.approval_handler.approve(
                    tool_name=normalized_call.name,
                    summary=str(normalized_call.arguments),
                )
                if not approved:
                    return ToolResult(ok=False, tool_name=normalized_call.name, error="Execução negada pelo usuário")
            handler = self.registry.get(normalized_call.name)
            return handler(normalized_call)
        except ToolPolicyError as exc:
            return ToolResult(ok=False, tool_name=normalized_call.name, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=normalized_call.name, error=f"Falha inesperada: {exc}")

    def maybe_execute_from_response(self, response: str | None) -> tuple[str | None, ToolResult | None]:
        """Tenta execute from response."""
        try:
            call = extract_tool_call(response)
        except ToolCallParseError as exc:
            return response, ToolResult(ok=False, tool_name="parse", error=str(exc))
        if call is None:
            return response, None
        result = self.execute(call)
        return response, result

    def _normalize_call(self, call: ToolCall) -> ToolCall:
        """Canoniza aliases conhecidos de tools para aumentar robustez com modelos menos estritos."""
        canonical_name = self._ALIASES.get(call.name, call.name)
        arguments = dict(call.arguments)

        if canonical_name == "run_shell" and "command" not in arguments:
            commands = arguments.get("commands")
            if isinstance(commands, list) and len(commands) == 1 and isinstance(commands[0], str):
                arguments["command"] = commands[0]

        if canonical_name == "exec_command" and "cmd" not in arguments and "command" in arguments:
            arguments["cmd"] = arguments["command"]

        return ToolCall(
            name=canonical_name,
            arguments=arguments,
            call_id=call.call_id,
            metadata=call.metadata,
        )
