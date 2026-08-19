"""Gerenciamento em runtime das conexões MCP usadas pelo Quimera como cliente."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quimera.config import ConfigManager
from quimera.runtime.mcp.client import (
    MCPClientBridge,
    _spec_name,
    merge_specs_by_name,
    parse_mcp_client_env_specs,
    parse_mcp_client_spec,
)
from quimera.runtime.tools.mcp_clients import (
    get_bridge,
    refresh_registration,
    set_bridge,
)


@dataclass(frozen=True)
class MCPConnectionInfo:
    """Visão de uma conexão persistida para apresentação na UI."""

    name: str
    transport: str
    endpoint: str
    connected: bool


def describe_mcp_client_spec(spec: str, *, connected: bool = False) -> MCPConnectionInfo:
    """Converte a spec persistida em campos amigáveis sem abrir conexão."""
    name = _spec_name(spec)
    rest = spec.split("=", 1)[1].strip() if "=" in spec else ""
    transport = "http"
    endpoint = rest
    if rest.startswith("http://") or rest.startswith("https://"):
        transport = "http"
    elif ":" in rest:
        transport, endpoint = rest.split(":", 1)
        transport = transport.strip().lower()
        endpoint = endpoint.strip()
    return MCPConnectionInfo(
        name=name,
        transport=transport,
        endpoint=endpoint,
        connected=connected,
    )


class MCPConnectionManager:
    """Fonte única para persistência e estado vivo dos MCP clients externos."""

    def __init__(self, *, config: ConfigManager, executor: Any) -> None:
        self.config = config
        self.executor = executor
        bridge = get_bridge()
        if bridge is None:
            bridge = MCPClientBridge()
            set_bridge(bridge)
        self.bridge = bridge

    @classmethod
    def from_app(cls, app: Any) -> "MCPConnectionManager":
        """Cria o manager usando o arquivo MCP específico do workspace atual."""
        existing = getattr(app, "mcp_connection_manager", None)
        if isinstance(existing, cls):
            return existing
        workspace = getattr(app, "workspace")
        manager = cls(
            config=ConfigManager(workspace.mcp_config_file),
            executor=getattr(app, "tool_executor"),
        )
        setattr(app, "mcp_connection_manager", manager)
        return manager

    def list_connections(self) -> list[MCPConnectionInfo]:
        """Lista configurações persistidas com o estado vivo da sessão."""
        sessions = self.bridge.sessions
        return [
            describe_mcp_client_spec(spec, connected=_spec_name(spec) in sessions)
            for spec in (self.config.mcp_clients or [])
        ]

    def upsert(
        self,
        spec: str,
        *,
        env_spec: str | None = None,
    ) -> MCPConnectionInfo:
        """Conecta/reconfigura uma conexão e persiste somente após sucesso."""
        name = _spec_name(spec)
        if not name:
            raise ValueError("Conexão MCP exige um nome")

        existing_specs = self.config.mcp_clients or []
        existing_env_specs = self.config.mcp_client_env or []
        merged_specs = merge_specs_by_name(existing_specs, [spec])

        env_specs = existing_env_specs
        if env_spec is not None:
            if env_spec.strip():
                env_specs = merge_specs_by_name(existing_env_specs, [env_spec])
            else:
                env_specs = [item for item in existing_env_specs if _spec_name(item) != name]

        env_overrides = parse_mcp_client_env_specs(env_specs)
        parsed_name, transport = parse_mcp_client_spec(spec, env_overrides)
        self.bridge.replace_connection(parsed_name, transport)
        refresh_registration(self.executor, self.bridge)

        self.config.set_mcp_clients(merged_specs)
        self.config.set_mcp_client_env(env_specs)
        return describe_mcp_client_spec(spec, connected=True)

    def reconnect(self, name: str) -> MCPConnectionInfo:
        """Refaz o handshake da conexão persistida sem alterar configuração."""
        spec = self._find_spec(name)
        env_overrides = parse_mcp_client_env_specs(self.config.mcp_client_env)
        parsed_name, transport = parse_mcp_client_spec(spec, env_overrides)
        self.bridge.replace_connection(parsed_name, transport)
        refresh_registration(self.executor, self.bridge)
        return describe_mcp_client_spec(spec, connected=True)

    def disconnect(self, name: str) -> bool:
        """Desconecta nesta sessão preservando a configuração persistida."""
        removed = self.bridge.disconnect_connection(name)
        refresh_registration(self.executor, self.bridge)
        return removed

    def remove(self, name: str) -> None:
        """Desconecta e remove a configuração persistida do workspace."""
        self.bridge.disconnect_connection(name)
        refresh_registration(self.executor, self.bridge)
        specs = [item for item in (self.config.mcp_clients or []) if _spec_name(item) != name]
        env_specs = [
            item for item in (self.config.mcp_client_env or []) if _spec_name(item) != name
        ]
        self.config.set_mcp_clients(specs)
        self.config.set_mcp_client_env(env_specs)

    def env_spec_for(self, name: str) -> str | None:
        """Retorna a configuração de ambiente persistida para a conexão."""
        for spec in self.config.mcp_client_env or []:
            if _spec_name(spec) == name:
                return spec
        return None

    def _find_spec(self, name: str) -> str:
        for spec in self.config.mcp_clients or []:
            if _spec_name(spec) == name:
                return spec
        raise KeyError(f"Conexão MCP não configurada: {name}")
