"""Registra tools de servidores MCP externos no ToolExecutor do Quimera.

Module-level singleton para o MCPClientBridge. Usado pelo executor
para registrar handlers que fazem proxy para servidores MCP externos.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quimera.runtime.mcp.client import MCPClientBridge
    from quimera.runtime.registry import ToolRegistry
    from quimera.runtime.policy import ToolPolicy
    from quimera.runtime.config import ToolRuntimeConfig

_logger = logging.getLogger(__name__)

_bridge: MCPClientBridge | None = None


class ExternalMCPToolValidator:
    """Validador estrutural para tools importadas de servidores MCP externos."""

    def validate(self, call) -> None:
        if not isinstance(call.arguments, dict):
            from quimera.runtime.policy import ToolPolicyError

            raise ToolPolicyError(
                f"Tool MCP externa requer argumentos em formato objeto: {call.name}"
            )


def set_bridge(bridge: MCPClientBridge | None) -> None:
    global _bridge
    _bridge = bridge


def get_bridge() -> MCPClientBridge | None:
    return _bridge


def register(
    registry: ToolRegistry,
    policy: ToolPolicy,
    config: ToolRuntimeConfig,
) -> None:
    """Registra handlers de servidores MCP externos no ToolRegistry.

    Chamado por ``ToolExecutor._register_builtin_tools()``.
    """
    bridge = _bridge
    if bridge is None:
        _logger.debug(
            "MCP client bridge não configurado — nenhuma tool externa registrada"
        )
        return

    registered = bridge.register_handlers(registry)
    if registered:
        policy.register_tool_validator(registered, ExternalMCPToolValidator())
        register_external = getattr(policy, "register_external_mcp_tools", None)
        if callable(register_external):
            register_external(registered)
        try:
            from quimera.runtime.drivers.tool_schemas import set_bridge_schemas

            set_bridge_schemas(bridge.get_schemas())
        except Exception:
            _logger.debug(
                "MCP bridge: falha ao publicar schemas externos",
                exc_info=True,
            )
        _logger.info(
            "MCP bridge: %d tools externas registradas: %s",
            len(registered),
            ", ".join(registered),
        )


def refresh_registration(executor, bridge: MCPClientBridge | None = None) -> list[str]:
    """Sincroniza as tools MCP externas do executor com o bridge atual.

    Remove handlers/schemas da configuração anterior e registra novamente as
    sessões atualmente conectadas. É o caminho usado pelo gerenciamento em
    runtime para conectar, reconectar e remover MCPs sem reiniciar o Quimera.
    """
    from quimera.runtime.drivers.tool_schemas import (
        get_bridge_schemas,
        set_bridge_schemas,
    )

    current_bridge = bridge if bridge is not None else _bridge
    previous_names = [
        str(schema.get("function", {}).get("name") or "")
        for schema in get_bridge_schemas()
    ]
    previous_names = [name for name in previous_names if name]
    executor.registry.unregister_many(previous_names)
    executor.policy.unregister_tool_validators(previous_names)
    executor.policy.unregister_external_mcp_tools(previous_names)

    if current_bridge is None:
        set_bridge_schemas([])
        return []

    registered = current_bridge.register_handlers(executor.registry)
    if registered:
        executor.policy.register_tool_validator(registered, ExternalMCPToolValidator())
        executor.policy.register_external_mcp_tools(registered)
    set_bridge_schemas(current_bridge.get_schemas())
    return registered
