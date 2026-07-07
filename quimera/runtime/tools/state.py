"""Ferramenta MCP para atualização do shared_state pelos agentes."""
from __future__ import annotations

from typing import Callable

from ..config import ToolRuntimeConfig
from ..models import ToolCall, ToolResult
from .base import ToolBase


class StateTools(ToolBase):
    """Ferramenta que permite a um agente atualizar o shared_state."""

    def __init__(self, config: ToolRuntimeConfig) -> None:
        """Inicializa uma instância de StateTools."""
        super().__init__(config)
        self._update_state_fn: Callable[[dict], bool] | None = None

    def set_update_state_fn(self, fn: Callable[[dict], bool]) -> None:
        """Injeta callable que aplica o payload de estado ao shared_state.

        Assinatura esperada: fn(payload: dict) -> bool
        """
        self._update_state_fn = fn

    def is_update_state_available(self) -> bool:
        """Indica se update_shared_state está operável no contexto atual."""
        return self._update_state_fn is not None

    def update_shared_state(self, call: ToolCall) -> ToolResult:
        """Mescla os campos informados no shared_state compartilhado da sessão.

        Aceita apenas chaves do contrato de agente (ver ``shared_state.AGENT_STATE_KEYS``);
        chaves fora do contrato ou com tipo inválido são silenciosamente ignoradas.
        """
        updates = call.arguments.get("updates")
        if not isinstance(updates, dict) or not updates:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="'updates' deve ser um objeto não vazio",
            )

        if self._update_state_fn is None:
            return ToolResult(
                ok=False,
                tool_name=call.name,
                error="update_shared_state não está disponível neste contexto",
            )

        try:
            applied = self._update_state_fn(updates)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, tool_name=call.name, error=f"Erro inesperado: {exc}")

        return ToolResult(
            ok=bool(applied),
            tool_name=call.name,
            content="ok" if applied else "nenhuma chave válida aplicada",
        )


def register(registry, policy, config: ToolRuntimeConfig) -> StateTools:
    """Registra update_shared_state no registry e retorna o objeto para injeção posterior."""
    tools = StateTools(config)
    registry.register("update_shared_state", tools.update_shared_state)
    return tools
