"""Componentes de `quimera.runtime.tools.search` (descoberta de tools diferidas)."""
from __future__ import annotations

import json
import logging
import weakref

from ..models import ToolCall, ToolResult
from ..policy import ToolPolicyError
from ..tool_deferral import (
    DEFERRAL_STATE_KEY,
    TOOL_SEARCH_DEFAULT_RESULTS,
    TOOL_SEARCH_MAX_RESULTS,
    TOOL_SEARCH_TOOL_NAME,
    match_deferred_schemas,
)
from .base import ValidatableTool

logger = logging.getLogger(__name__)

_TOOL_SEARCH_NAMES = [TOOL_SEARCH_TOOL_NAME]


class ToolSearchTools:
    """Resolve pedidos de descoberta e habilita tools diferidas por escopo.

    No caminho MCP o escopo é o estado da conexão (``_mcp_state``), atualizado
    aqui e lido pelo ``tools/list`` do servidor. No caminho dos drivers de API
    o loop de tool calling lê ``data["enabled_tools"]`` do resultado e expande
    o array de tools do hop seguinte.
    """

    def __init__(self, config) -> None:
        self._config = config
        self._executor_ref: weakref.ref | None = None

    def set_executor(self, executor) -> None:
        """Vincula o executor dono do registry para resolver o catálogo real."""
        self._executor_ref = weakref.ref(executor)

    def _resolved_schemas(self) -> list[dict]:
        from ..drivers.tool_schemas import resolve_tool_schemas

        executor = self._executor_ref() if self._executor_ref is not None else None
        return resolve_tool_schemas(executor)

    def tool_search(self, call: ToolCall) -> ToolResult:
        """Retorna schemas completos das tools pedidas e as habilita no escopo."""
        names = [str(name) for name in call.arguments.get("names") or []]
        query = str(call.arguments.get("query") or "").strip()
        max_results = call.arguments.get("max_results", TOOL_SEARCH_DEFAULT_RESULTS)

        outcome = match_deferred_schemas(
            self._resolved_schemas(),
            names=names,
            query=query or None,
            max_results=int(max_results),
        )

        enabled = outcome["enabled"]
        self._enable_in_mcp_state(call, enabled)

        tools_payload = [
            {
                "name": schema.get("function", {}).get("name"),
                "description": schema.get("function", {}).get("description"),
                "parameters": schema.get("function", {}).get("parameters"),
            }
            for schema in outcome["matched"]
        ]
        payload: dict = {"tools": tools_payload}
        if enabled:
            payload["enabled"] = enabled
            payload["note"] = (
                "As ferramentas acima já estão habilitadas e podem ser chamadas "
                "a partir do próximo passo."
            )
        if outcome["already_available"]:
            payload["already_available"] = outcome["already_available"]
        if outcome["unknown"]:
            payload["unknown"] = outcome["unknown"]
        if not tools_payload:
            payload["note"] = (
                "Nenhuma ferramenta corresponde ao pedido. Refine a query ou "
                "consulte o catálogo na descrição de tool_search."
            )

        return ToolResult(
            ok=True,
            tool_name=call.name,
            content=json.dumps(payload, ensure_ascii=False),
            data={
                "enabled_tools": enabled,
                "already_available": outcome["already_available"],
                "unknown_tools": outcome["unknown"],
            },
        )

    @staticmethod
    def _enable_in_mcp_state(call: ToolCall, enabled: list[str]) -> None:
        """Acumula habilitações no estado da conexão MCP, quando presente.

        A substituição por um frozenset novo mantém a leitura concorrente do
        ``tools/list`` sempre sobre um snapshot consistente.
        """
        if not enabled:
            return
        metadata = call.metadata if isinstance(call.metadata, dict) else {}
        state = metadata.get("_mcp_state")
        if not isinstance(state, dict):
            return
        current = state.get(DEFERRAL_STATE_KEY) or frozenset()
        state[DEFERRAL_STATE_KEY] = frozenset(current) | frozenset(enabled)


class ToolSearchValidator(ValidatableTool):
    """Valida apenas argumentos; a tool é estritamente read-only."""

    def _validate_tool_search(self, call: ToolCall) -> None:
        names = call.arguments.get("names")
        query = call.arguments.get("query")
        if names is not None:
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ToolPolicyError("tool_search.names deve ser uma lista de strings")
        if query is not None and not isinstance(query, str):
            raise ToolPolicyError("tool_search.query deve ser string")
        if not names and not str(query or "").strip():
            raise ToolPolicyError("tool_search exige 'names' e/ou 'query'")
        max_results = call.arguments.get("max_results")
        if max_results is not None:
            if not isinstance(max_results, int) or isinstance(max_results, bool):
                raise ToolPolicyError("tool_search.max_results deve ser inteiro")
            if not 1 <= max_results <= TOOL_SEARCH_MAX_RESULTS:
                raise ToolPolicyError(
                    "tool_search.max_results deve estar entre 1 e "
                    f"{TOOL_SEARCH_MAX_RESULTS}"
                )


def register(registry, policy, config) -> ToolSearchTools:
    """Registra a tool de descoberta de ferramentas diferidas."""
    tools = ToolSearchTools(config)
    validator = ToolSearchValidator(config)
    for name in _TOOL_SEARCH_NAMES:
        registry.register(name, getattr(tools, name))
    policy.register_tool_validator(_TOOL_SEARCH_NAMES, validator)
    return tools
