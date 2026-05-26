"""Componentes de `quimera.runtime.executor`."""
from __future__ import annotations

from .config import ToolRuntimeConfig
from .models import ToolCall, ToolResult
from .parser import ToolCallParseError, extract_tool_call
from .policy import ToolPolicy, ToolPolicyError
from .registry import ToolRegistry
from .tools.files import FileTools
from .tools.patch import PatchTool
from .tools.shell import ShellTool
from .tools.web import WebTool
from .tools.tasks import TaskTools
from .approve_summary import ApproveSummary


class ToolExecutor:
    """Executa um loop simples de tool calling com validação e aprovação."""

    _ALIASES = {
        "run": "run_shell",
        "execute_command": "exec_command",
    }

    def __init__(
            self,
            config: ToolRuntimeConfig,
            approval_handler,
            registry: ToolRegistry | None = None,
            policy: ToolPolicy | None = None,
    ) -> None:
        """Inicializa uma instância de ToolExecutor."""
        self.config = config
        self._approval_handler = approval_handler
        self.registry = registry or ToolRegistry()
        self.policy = policy or ToolPolicy(config)
        self._tool_preview_callback = None
        self._call_agent_fn = None
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        """Executa register builtin tools."""
        file_tools = FileTools(self.config)
        patch_tool = PatchTool(self.config)
        shell_tool = ShellTool(self.config)
        web_tool = WebTool(self.config)
        task_tools = TaskTools(self.config)
        self.registry.register("list_files", file_tools.list_files)
        self.registry.register("read_file", file_tools.read_file)
        self.registry.register("write_file", file_tools.write_file)
        self.registry.register("remove_file", file_tools.remove_file)
        self.registry.register("apply_patch", patch_tool.apply_patch)
        self.registry.register("grep_search", file_tools.grep_search)
        self.registry.register("run_shell", shell_tool.run_shell)
        self.registry.register("exec_command", shell_tool.exec_command)
        self.registry.register("write_stdin", shell_tool.write_stdin)
        self.registry.register("close_command_session", shell_tool.close_command_session)
        # Task-related read-only tools
        self.registry.register("list_tasks", task_tools.list_tasks)
        self.registry.register("web_search", web_tool.web_search)
        self.registry.register("web_fetch", web_tool.web_fetch)
        self.registry.register("list_jobs", task_tools.list_jobs)
        self.registry.register("get_job", task_tools.get_job)
        self.registry.register("call_agent", self._handle_agent_dispatch)

    @property
    def approval_handler(self):
        """Acesso ao handler de aprovação (para pré-aprovação externa)."""
        return self._approval_handler

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn):
        """Injeta callbacks de spinner no approval handler.

        Encadeia até o handler base (ConsoleApprovalHandler) atravessando
        possíveis wrappers como PreApprovalHandler.
        """
        handler = self._approval_handler
        # Atravessa wrappers (ex: PreApprovalHandler) até chegar no base
        while hasattr(handler, '_base'):
            handler = handler._base
        setter = getattr(handler, 'set_spinner_callbacks', None)
        if callable(setter):
            setter(suspend_spinner_fn, resume_spinner_fn)

    def set_approval_cancel_event(self, cancel_event) -> None:
        """Injeta cancel_event no approval handler base quando suportado."""
        handler = self._approval_handler
        while hasattr(handler, '_base'):
            handler = handler._base
        setter = getattr(handler, "set_cancel_event", None)
        if callable(setter):
            setter(cancel_event)

    def get_thread_approval_scope(self) -> str | None:
        """Lê o escopo de aprovação propagável da thread atual."""
        getter = getattr(self._approval_handler, "get_thread_approval_scope", None)
        if callable(getter):
            return getter()
        return None

    def bind_thread_approval_scope(self, scope_key: str | None) -> str | None:
        """Associa temporariamente um escopo de aprovação à thread atual."""
        binder = getattr(self._approval_handler, "bind_thread_approval_scope", None)
        if callable(binder):
            return binder(scope_key)
        return None

    def would_require_approval(self, call: ToolCall) -> bool:
        """Retorna True se a chamada passaria pelo fluxo de aprovação.

        Consolida a lógica de permissão sem duplicar as regras de política.
        Usado externamente (ex: preview de tools) para saber se o executor
        vai pedir aprovação antes de executar.
        """
        normalized_call = self._normalize_call(call)
        try:
            self.policy.validate(normalized_call)
            permission_error = self.policy.check_path_permission(normalized_call)
            needs_approval = self.policy.requires_approval(normalized_call)
            return permission_error is not None or bool(needs_approval)
        except Exception:
            # Se validation falha, consideramos que precisa de aprovação
            # (seguro: mostra approval mesmo que não precise, nunca o contrário)
            return True

    def set_tool_preview_callback(self, fn) -> None:
        """Registra um callback chamado antes de executar tools que NÃO passam por approval.
        Assinatura esperada: fn(tool_name: str, arguments: dict) -> None
        """
        self._tool_preview_callback = fn

    def set_call_agent_fn(self, fn) -> None:
        """Injeta callable para despachar tarefas a outro agente.

        Assinatura esperada: fn(agent_name: str, **options) -> str | None
        """
        self._call_agent_fn = fn

    def is_call_agent_available(self) -> bool:
        """Indica se a tool call_agent está operável no contexto atual."""
        return callable(self._call_agent_fn)

    def _handle_agent_dispatch(self, call: ToolCall) -> ToolResult:
        """Dispatch a task to another Quimera agent via MCP tool."""
        if not self.is_call_agent_available():
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Agent dispatch not available in this context",
            )
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        agent_name_raw = arguments.get("agent_name")
        task_raw = arguments.get("task")
        context_raw = arguments.get("context")

        agent_name = str(agent_name_raw).strip() if isinstance(agent_name_raw, str) else ""
        task = str(task_raw).strip() if isinstance(task_raw, str) else ""
        context = ""
        if context_raw is not None:
            context = str(context_raw).strip() if isinstance(context_raw, str) else str(context_raw)

        if not agent_name or not task:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="Both 'agent_name' and 'task' are required",
            )
        handoff = {
            "task": task,
            "context": context,
        }
        try:
            result = self._call_agent_fn(
                agent_name,
                handoff=handoff,
                handoff_only=True,
                protocol_mode="handoff",
                primary=False,
                silent=True,
                show_output=False,
                persist_history=True,
            )
            if result is None:
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error=f"Agent '{agent_name}' returned no response",
                )
            return ToolResult(ok=True, tool_name=call.name, content=str(result))
        except Exception as e:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error=str(e),
            )

    def execute(self, call: ToolCall) -> ToolResult:
        """Executa um ToolCall com política de aprovação.

        Se aprovação for necessária, bloqueia no approval_handler
        até obter decisão do humano. O handler pode ser interativo
        (ConsoleApprovalHandler com input()) ou automático
        (AutoApprovalHandler para testes).
        """
        normalized_call = self._normalize_call(call)
        try:
            self.policy.validate(normalized_call)

            # Verifica se precisa de aprovação (mutação ou permissão)
            permission_error = self.policy.check_path_permission(normalized_call)
            needs_approval = self.policy.requires_approval(normalized_call)
            has_permission_issue = permission_error is not None

            if has_permission_issue or needs_approval:
                # Bloqueia no handler de aprovação (pode ser interativo)
                approved = self._approval_handler.approve(
                    tool_name=normalized_call.name,
                    summary=(
                        f"Permissão necessária para acessar: {permission_error.resolved_path}"
                        if has_permission_issue
                        else ApproveSummary.build(normalized_call.name, normalized_call.arguments)
                    ),
                )
                if not approved:
                    return ToolResult(
                        ok=False,
                        tool_name=normalized_call.name,
                        error="Execução negada pelo usuário",
                    )
            else:
                # Tool sem approval: exibe preview informativo se houver callback
                if self._tool_preview_callback is not None:
                    self._tool_preview_callback(normalized_call.name, normalized_call.arguments)

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
