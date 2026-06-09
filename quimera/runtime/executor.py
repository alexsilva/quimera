"""Componentes de `quimera.runtime.executor`."""
from __future__ import annotations

from typing import Callable

from .config import ToolRuntimeConfig
from .models import ToolCall, ToolResult
from .policy import ToolPolicy, ToolPolicyError
from .registry import ToolRegistry
from .tools.files import FileTools
from .tools.patch import PatchTool
from .tools.shell import ShellTool
from .tools.web import WebTool
from .tools.tasks import TaskTools
from .tools.handoff import HandoffTools
from .tools.todo import TodoTools
from .approval_broker import ApprovalBroker


class ToolExecutor:
    """Executa chamadas estruturadas de ferramentas com validação e aprovação."""

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
        self._tool_progress_callback = None
        self.approval_broker = ApprovalBroker(config, approval_handler)
        self._task_tools = TaskTools(self.config)
        self._handoff_tools = HandoffTools(self.config)
        self._todo_tools = TodoTools(self.config)
        self._register_builtin_tools()

    def set_tool_progress_callback(self, fn) -> None:
        """Registra um callback para reporte de progresso durante a execução de tools.
        Assinatura esperada: fn(message: str) -> None
        """
        self._tool_progress_callback = fn
        self._handoff_tools.set_progress_callback(fn)

    def _register_builtin_tools(self) -> None:
        """Executa register builtin tools."""
        file_tools = FileTools(self.config)
        patch_tool = PatchTool(self.config)
        shell_tool = ShellTool(self.config)
        web_tool = WebTool(self.config)
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
        self.registry.register("list_tasks", self._task_tools.list_tasks)
        self.registry.register("web_search", web_tool.web_search)
        self.registry.register("web_fetch", web_tool.web_fetch)
        self.registry.register("list_jobs", self._task_tools.list_jobs)
        self.registry.register("get_job", self._task_tools.get_job)
        self.registry.register("call_agent", self._handoff_tools.call_agent)
        self.registry.register("list_agents", self._handoff_tools.list_agents)
        self.registry.register("todo_write", self._todo_tools.todo_write)
        self.registry.register("todo_list", self._todo_tools.todo_list)

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
            if self.policy.requires_validation(normalized_call):
                self.policy.validate(normalized_call)
            permission_error = None
            if self.policy.requires_path_permission(normalized_call):
                permission_error = self.policy.check_path_permission(normalized_call)
            needs_approval = self.policy.requires_approval(normalized_call)
            return self.approval_broker.should_request_approval(
                normalized_call,
                needs_policy_approval=bool(needs_approval),
                permission_error=permission_error,
            )
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
        self._handoff_tools.set_call_agent_fn(fn)

    def set_active_agents_provider(self, fn) -> None:
        """Injeta provider que retorna agentes ativos no momento da delegação."""
        self._handoff_tools.set_active_agents_provider(fn)

    def is_call_agent_available(self) -> bool:
        """Indica se a tool call_agent está operável no contexto atual."""
        return self._handoff_tools.is_call_agent_available()

    def execute(self, call: ToolCall, progress_callback: Callable[[str], None] | None = None) -> ToolResult:
        """Executa um ToolCall com política de aprovação.

        Se aprovação for necessária, bloqueia no approval_handler
        até obter decisão do humano. O handler pode ser interativo
        (ConsoleApprovalHandler com input()) ou automático
        (AutoApprovalHandler para testes).
        """
        normalized_call = self._normalize_call(call)
        try:
            # Sincroniza callback de progresso se fornecido (prioridade sobre o global)
            effective_progress_callback = progress_callback or self._tool_progress_callback
            if effective_progress_callback:
                self._handoff_tools.set_progress_callback(effective_progress_callback)
            if self.policy.requires_validation(normalized_call):
                self.policy.validate(normalized_call)

            # Verifica se precisa de aprovação (mutação ou permissão)
            permission_error = None
            if self.policy.requires_path_permission(normalized_call):
                permission_error = self.policy.check_path_permission(normalized_call)
            needs_approval = self.policy.requires_approval(normalized_call)
            has_permission_issue = permission_error is not None

            approved = self.approval_broker.approve(
                normalized_call,
                needs_policy_approval=bool(needs_approval),
                permission_error=permission_error,
            )
            if not approved:
                return ToolResult(
                    ok=False,
                    tool_name=normalized_call.name,
                    error="Execução negada pelo usuário",
                )

            if not (has_permission_issue or needs_approval):
                # Tool sem approval humano: exibe preview informativo se houver callback
                if self._tool_preview_callback is not None:
                    self._tool_preview_callback(normalized_call.name, normalized_call.arguments)

            handler = self.registry.get(normalized_call.name)
            with self.approval_broker.execution_guard(normalized_call):
                return handler(normalized_call)
        except ToolPolicyError as exc:
            return ToolResult(ok=False, tool_name=normalized_call.name, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=normalized_call.name, error=f"Falha inesperada: {exc}")

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
