"""Gerenciamento em runtime das conexões MCP usadas pelo Quimera como cliente."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from quimera.config import ConfigManager
from quimera.runtime.mcp.client import (
    MCPClientBridge,
    MCPConnectionPhase,
    MCPConnectionState,
    _spec_name,
    bind_bridge_notices,
    connect_mcp_client_spec,
    merge_specs_by_name,
    parse_mcp_client_env_specs,
    parse_mcp_client_spec,
    spec_transport_type,
)
from quimera.runtime.tools.mcp_clients import (
    get_bridge,
    refresh_registration,
    set_bridge,
)


@dataclass(frozen=True)
class MCPConnectionInfo:
    """Visão de uma conexão persistida para apresentação na UI.

    ``phase`` e ``detail`` espelham o estado vivo publicado pelo bridge
    (``connecting``, ``failed`` com o erro, ...); ficam vazios quando a
    conexão nunca foi tentada nesta sessão.
    """

    name: str
    transport: str
    endpoint: str
    connected: bool
    phase: str = ""
    detail: str = ""
    tools: int = 0
    auth_url: str = ""

    @property
    def in_progress(self) -> bool:
        return self.phase in {MCPConnectionPhase.PENDING, MCPConnectionPhase.CONNECTING}

    @property
    def failed(self) -> bool:
        return self.phase == MCPConnectionPhase.FAILED

    @property
    def auth_pending(self) -> bool:
        """O servidor pediu uma autorização OAuth no navegador ainda não concluída."""
        return bool(self.auth_url)

    @property
    def established(self) -> bool:
        """Conexão utilizável: sessão viva no bridge ou fase ``connected``."""
        return self.connected or self.phase == MCPConnectionPhase.CONNECTED

    @property
    def state_label(self) -> str:
        """Rótulo curto de estado para tabelas e resumos."""
        if self.auth_pending and not self.failed:
            return "autorização pendente"
        if self.phase == MCPConnectionPhase.CONNECTING:
            return "conectando…"
        if self.phase == MCPConnectionPhase.PENDING:
            return "aguardando…"
        if self.phase == MCPConnectionPhase.FAILED:
            return "falha"
        if self.established:
            return "conectado"
        return "offline"


def describe_mcp_client_spec(
    spec: str,
    *,
    connected: bool = False,
    state: MCPConnectionState | None = None,
) -> MCPConnectionInfo:
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
        phase=state.phase if state is not None else "",
        detail=state.detail if state is not None else "",
        tools=state.tools if state is not None else 0,
        auth_url=state.auth_url if state is not None else "",
    )


logger = logging.getLogger(__name__)


class MCPConnectionManager:
    """Fonte única para persistência e estado vivo dos MCP clients externos."""

    def __init__(self, *, executor: Any, workspace) -> None:
        self.executor = executor
        self.workspace = workspace
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
            executor=getattr(app, "tool_executor"),
            workspace=workspace,
        )
        setattr(app, "mcp_connection_manager", manager)
        return manager

    @property
    def config(self) -> ConfigManager:
        """Configuração MCP resolvida a partir do Workspace atual."""
        return ConfigManager(self.workspace.mcp_connections_file)

    def list_connections(self) -> list[MCPConnectionInfo]:
        """Lista configurações persistidas com o estado vivo da sessão."""
        sessions = self.bridge.sessions
        states = self.bridge.states()
        return [
            describe_mcp_client_spec(
                spec,
                connected=_spec_name(spec) in sessions,
                state=states.get(_spec_name(spec)),
            )
            for spec in (self.config.mcp_clients or [])
        ]

    def subscribe(self, listener) -> Any:
        """Observa mudanças de estado das conexões; retorna o cancelamento."""
        return self.bridge.subscribe(listener)

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
        parsed_name, transport = parse_mcp_client_spec(
            spec,
            env_overrides,
            workspace=self.workspace,
        )
        bind_bridge_notices(self.bridge, parsed_name, transport)
        self.bridge.replace_connection(parsed_name, transport)
        refresh_registration(self.executor, self.bridge)

        self.config.set_mcp_configuration(merged_specs, env_specs)
        return describe_mcp_client_spec(
            spec, connected=True, state=self.bridge.state(parsed_name)
        )

    def reconnect(self, name: str) -> MCPConnectionInfo:
        """Refaz o handshake da conexão persistida sem alterar configuração.

        Bloqueia até o handshake terminar; a UI usa
        :meth:`reconnect_in_background`, que não segura a tela enquanto o
        servidor espera (ex.: autorização OAuth no navegador).
        """
        spec = self._find_spec(name)
        env_overrides = parse_mcp_client_env_specs(self.config.mcp_client_env)
        parsed_name, transport = parse_mcp_client_spec(
            spec,
            env_overrides,
            workspace=self.workspace,
        )
        bind_bridge_notices(self.bridge, parsed_name, transport)
        self.bridge.replace_connection(parsed_name, transport)
        refresh_registration(self.executor, self.bridge)
        return describe_mcp_client_spec(
            spec, connected=True, state=self.bridge.state(parsed_name)
        )

    def reconnect_in_background(
        self,
        name: str,
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> threading.Thread:
        """Inicia o handshake de ``name`` em uma thread e retorna imediatamente.

        Um handshake anterior ainda em andamento (ex.: OAuth nunca concluído)
        é abandonado pelo bridge. O progresso vai para o estado da conexão
        (``connecting`` → ``connected``/``failed``), que a UI acompanha via
        :meth:`subscribe`. Levanta ``KeyError`` se a conexão não existe.
        """
        spec = self._find_spec(name)
        parsed_name = _spec_name(spec)
        if parsed_name not in self.bridge.sessions:
            # Feedback imediato na UI; com sessão viva o estado segue
            # "connected" até a troca transacional terminar.
            self.bridge.mark_connecting(parsed_name, spec_transport_type(spec))
        return self._start_connection_thread(spec, thread_factory=thread_factory)

    def upsert_in_background(
        self,
        spec: str,
        *,
        env_spec: str | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> MCPConnectionInfo:
        """Valida e persiste a conexão, depois conecta em background.

        Diferente de :meth:`upsert`, a configuração é gravada antes do
        handshake: uma conexão que exige autorização OAuth não pode segurar o
        editor até o usuário concluir no navegador. Specs inválidas levantam
        ``ValueError`` antes de qualquer persistência.
        """
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
        parsed_name, transport = parse_mcp_client_spec(
            spec,
            env_overrides,
            workspace=self.workspace,
        )
        self.config.set_mcp_configuration(merged_specs, env_specs)
        self.bridge.mark_pending(parsed_name, transport.transport_type)
        self._start_connection_thread(spec, thread_factory=thread_factory)
        return describe_mcp_client_spec(
            spec, connected=False, state=self.bridge.state(parsed_name)
        )

    def _start_connection_thread(
        self,
        spec: str,
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> threading.Thread:
        env_overrides = parse_mcp_client_env_specs(self.config.mcp_client_env)
        name = _spec_name(spec)
        thread = thread_factory(
            target=connect_mcp_client_spec,
            args=(self.bridge, spec),
            kwargs={
                "env_overrides": env_overrides,
                "workspace": self.workspace,
                "executor": self.executor,
            },
            name=f"quimera-mcp-client-{name}",
            daemon=True,
        )
        thread.start()
        return thread

    def disconnect(self, name: str) -> bool:
        """Desconecta nesta sessão preservando a configuração persistida."""
        removed = self.bridge.disconnect_connection(name)
        refresh_registration(self.executor, self.bridge)
        return removed

    def remove(self, name: str) -> None:
        """Desconecta e remove a configuração persistida do workspace."""
        self.bridge.disconnect_connection(name, forget=True)
        refresh_registration(self.executor, self.bridge)
        specs = [item for item in (self.config.mcp_clients or []) if _spec_name(item) != name]
        env_specs = [
            item for item in (self.config.mcp_client_env or []) if _spec_name(item) != name
        ]
        self.config.set_mcp_configuration(specs, env_specs)

    def env_text_for(self, name: str) -> str:
        """Retorna os pares ``KEY=valor`` persistidos para a conexão (vazio se não há)."""
        for spec in self.config.mcp_client_env or []:
            if _spec_name(spec) == name:
                return spec.split("=", 1)[1].strip() if "=" in spec else ""
        return ""

    def _find_spec(self, name: str) -> str:
        for spec in self.config.mcp_clients or []:
            if _spec_name(spec) == name:
                return spec
        raise KeyError(f"Conexão MCP não configurada: {name}")
