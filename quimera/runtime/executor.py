"""Componentes de `quimera.runtime.executor`."""
from __future__ import annotations

from typing import Callable

from .config import ToolRuntimeConfig
from .models import ToolCall, ToolResult
from .policy import ToolPolicy, ToolPolicyError
from .registry import ToolRegistry
from .tools import delegate as delegate_module
from .tools import files as files_tools
from .tools import git
from .tools import interaction as interaction_tools
from .tools import memory as memory_tools
from .tools import patch as patch_tools
from .tools import shell as shell_tools
from .tools import tasks as tasks_tools
from .tools import todo as todo_tools
from .tools import web as web_tools
from .approval import ApprovalHandler, ApprovalManager


class ToolExecutor:
    """Executa chamadas estruturadas de ferramentas com validação e aprovação."""

    _ALIASES = {
        "run": "run_shell",
        "execute_command": "exec_command",
    }

    def __init__(
            self,
            config: ToolRuntimeConfig,
            approval_handler: ApprovalManager | ApprovalHandler | None,
            registry: ToolRegistry | None = None,
            policy: ToolPolicy | None = None,
    ) -> None:
        """Inicializa uma instância de ToolExecutor."""
        self.config = config
        self.approval_manager = self._coerce_approval_manager(config, approval_handler)
        self._approval_handler = approval_handler or self.approval_manager
        # Alias público mantido para compatibilidade. O runtime interno usa o
        # manager; quem precisa da engine canônica pode acessar
        # ``approval_manager.governance`` explicitamente.
        self.approval_broker = self.approval_manager
        self.approval_governance = self.approval_manager.governance
        self.registry = registry or ToolRegistry()
        self.policy = policy or ToolPolicy(config)
        self._tool_preview_callback = None
        self._tool_progress_callback = None
        self._delegate_tools = None
        self._interaction_tools = None
        self._register_builtin_tools()

    @staticmethod
    def _coerce_approval_manager(
        config: ToolRuntimeConfig,
        approval_handler: ApprovalManager | ApprovalHandler | None,
    ) -> ApprovalManager:
        if isinstance(approval_handler, ApprovalManager):
            return approval_handler
        return ApprovalManager(config, base_handler=approval_handler)

    def set_tool_progress_callback(self, fn) -> None:
        """Registra um callback para reporte de progresso durante a execução de tools.
        Assinatura esperada: fn(message: str) -> None
        """
        self._tool_progress_callback = fn
        self._delegate_tools.set_progress_callback(fn)

    def _register_builtin_tools(self) -> None:
        """Registra todas as ferramentas builtin usando os módulos register()."""
        files_tools.register(self.registry, self.policy, self.config)
        patch_tools.register(self.registry, self.policy, self.config)
        shell_tools.register(self.registry, self.policy, self.config)
        web_tools.register(self.registry, self.policy, self.config)
        tasks_tools.register(self.registry, self.policy, self.config)
        todo_tools.register(self.registry, self.policy, self.config)
        memory_tools.register(self.registry, self.policy, self.config)
        self._delegate_tools = delegate_module.register(self.registry, self.policy, self.config)
        self._interaction_tools = interaction_tools.register(self.registry, self.policy, self.config)
        git.register(self.registry, self.policy, self.config)

    @property
    def approval_handler(self):
        """Acesso ao handler de aprovação (para pré-aprovação externa)."""
        return self._approval_handler

    def set_spinner_callbacks(self, suspend_spinner_fn, resume_spinner_fn):
        self.approval_manager.set_spinner_callbacks(suspend_spinner_fn, resume_spinner_fn)

    def set_approval_cancel_event(self, cancel_event) -> None:
        self.approval_manager.set_cancel_event(cancel_event)

    def process_pending_input_once(self) -> bool:
        """Processa uma pergunta pendente do InputBroker na thread atual."""
        process = getattr(self.approval_manager, "process_pending_input_once", None)
        if callable(process):
            return bool(process())
        return False

    def reset_approval_cycle(self) -> None:
        """Reseta estado de approve-all não-permanente ao fim do ciclo de tool hops."""
        self.approval_manager.reset_approve_all_after_cycle()

    def get_thread_approval_scope(self) -> str | None:
        """Lê o escopo de aprovação propagável da thread atual."""
        return self.approval_manager.get_thread_approval_scope()

    def bind_thread_approval_scope(self, scope_key: str | None) -> str | None:
        """Associa temporariamente um escopo de aprovação à thread atual."""
        return self.approval_manager.bind_thread_approval_scope(scope_key)

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
            return self.approval_manager.would_prompt_for_call(
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

    def set_delegate_fn(self, fn) -> None:
        """Injeta callable para despachar tarefas a outro agente.

        Assinatura esperada: fn(agent_name: str, **options) -> str | None
        """
        self._delegate_tools.set_delegate_fn(fn)

    def set_background_delegate_fn(self, fn) -> None:
        """Injeta callable isolado para delegação assíncrona via HTTP MCP.

        Deve usar dispatch services com AgentClient próprio para isolar
        cancelamentos do fluxo do chat das execuções assíncronas.
        """
        self._delegate_tools.set_background_delegate_fn(fn)

    def set_active_agents_provider(self, fn) -> None:
        """Injeta provider que retorna agentes ativos no momento da delegação."""
        self._delegate_tools.set_active_agents_provider(fn)

    def set_cancel_checker(self, fn) -> None:
        """Injeta checker de cancelamento para tools longas como delegate."""
        self._delegate_tools.set_cancel_checker(fn)

    def set_agent_cleanup_callback(self, fn) -> None:
        """Injeta callback para limpeza do estado de render após delegate.

        Assinatura esperada: fn(agent_name: str) -> None
        Chamado após cada step de delegate para limpar streams transitórios.
        """
        self._delegate_tools.set_cleanup_callback(fn)

    def is_delegate_available(self) -> bool:
        """Indica se a tool delegate está operável no contexto atual."""
        return self._delegate_tools.is_delegate_available()

    def set_ask_user_fn(self, fn) -> None:
        """Injeta callable que exibe pergunta com opções e lê a resposta do terminal.

        Assinatura esperada: fn(question: str, options: list[str]) -> (index: int, value: str)
        """
        self._interaction_tools.set_ask_user_fn(fn)

    def is_ask_user_available(self) -> bool:
        """Indica se ask_user está operável no contexto atual."""
        return self._interaction_tools.is_ask_user_available()

    def execute(self, call: ToolCall, progress_callback: Callable[[str], None] | None = None) -> ToolResult:
        """Executa um ToolCall com política de aprovação.

        Se aprovação for necessária, bloqueia no approval_handler
        até obter decisão do humano (ApprovalManager).
        """
        normalized_call = self._normalize_call(call)
        try:
            # Sincroniza callback de progresso se fornecido (prioridade sobre o global)
            effective_progress_callback = progress_callback or self._tool_progress_callback
            if effective_progress_callback:
                self._delegate_tools.set_progress_callback(effective_progress_callback)
            if self.policy.requires_validation(normalized_call):
                self.policy.validate(normalized_call)

            # Verifica se precisa de aprovação (mutação ou permissão)
            permission_error = None
            if self.policy.requires_path_permission(normalized_call):
                permission_error = self.policy.check_path_permission(normalized_call)
            needs_approval = self.policy.requires_approval(normalized_call)
            has_permission_issue = permission_error is not None

            approved = self.approval_manager.authorize_call(
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
            with self.approval_manager.guard_execution(normalized_call):
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
