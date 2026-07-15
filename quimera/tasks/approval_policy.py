"""Política de auto-approval de tools durante execução de tasks.

``TaskApprovalPolicy`` encapsula a lógica de habilitar/desabilitar
auto-approval de mutações no ``ApprovalManager`` para threads de
execução de tasks, eliminando duplicação entre ``AppTaskServices``
e ``TaskExecutorPool``.
"""

from __future__ import annotations

from typing import Any, Callable


class TaskApprovalPolicy:
    """Encapsula auto-approval de tools durante execução de tasks.

    A política mantém um identificador de ``owner_id`` (por padrão o id
    da própria instância) para compor ``scope_key`` exclusivos, evitando
    colisões entre diferentes instâncias que compartilham o mesmo
    ``ApprovalManager``.
    """

    def __init__(
        self,
        get_approval_handler: Callable[[], Any],
        owner_id: int | None = None,
    ) -> None:
        """Inicializa a política de aprovação.

        Parâmetros:
        - ``get_approval_handler``: callable que retorna o
          ``ApprovalManager`` primário atual.
        - ``owner_id``: id para compor o scope_key; por padrão
          ``id(self)``.
        """
        self._get_approval_handler = get_approval_handler
        self._owner_id = owner_id if owner_id is not None else id(self)

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def enable(self, agent_name: str, approval_handler: Any | None = None) -> None:
        """Habilita auto-approval para a thread do agente informado."""
        for handler in self._resolve_handlers(approval_handler):
            handler.set_thread_approve_all(
                True,
                scope_key=f"task:{agent_name}:{self._owner_id}",
                silent=True,
            )

    def disable(self, agent_name: str, approval_handler: Any | None = None) -> None:
        """Desabilita auto-approval para a thread do agente informado."""
        for handler in self._resolve_handlers(approval_handler):
            handler.set_thread_approve_all(
                False,
                scope_key=f"task:{agent_name}:{self._owner_id}",
            )

    def _resolve_handlers(
        self, approval_handler: Any | None = None
    ) -> list[Any]:
        """Retorna lista deduplicada de handlers válidos.

        A ordem prioriza o ``approval_handler`` explícito (tipicamente
        o ``approval_handler`` do ``ToolExecutor`` de background) e,
        por fim, o handler primário da política.
        """
        handlers: list[Any] = []
        primary = self._get_approval_handler()
        for handler in (approval_handler, primary):
            if handler is None or not hasattr(handler, "set_thread_approve_all"):
                continue
            if any(existing is handler for existing in handlers):
                continue
            handlers.append(handler)
        return handlers

    def make_hooks(
        self, tool_executor_getter: Callable[[], Any | None]
    ) -> tuple[
        Callable[[str], None],
        Callable[[str], None],
    ]:
        """Cria par (before_agent_call, after_agent_call) para ``TaskRunner``.

        ``tool_executor_getter`` é um callable que retorna o
        ``ToolExecutor`` de background atual, de onde se extrai o
        ``approval_handler`` específico do executor.
        """
        before = lambda agent_name: self.enable(  # noqa: E731
            agent_name,
            approval_handler=getattr(
                tool_executor_getter(), "approval_handler", None
            ),
        )
        after = lambda agent_name: self.disable(  # noqa: E731
            agent_name,
            approval_handler=getattr(
                tool_executor_getter(), "approval_handler", None
            ),
        )
        return before, after
