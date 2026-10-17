"""Gerenciamento em runtime das conexões MCP usadas pelo Quimera como cliente."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from quimera.runtime.mcp.client import (
    MCPClientBridge,
    MCPConnectionPhase,
    MCPConnectionState,
    _spec_name,
    bind_bridge_notices,
    connect_mcp_client_spec,
    parse_mcp_client_env_specs,
    parse_mcp_client_spec,
    spec_transport_type,
)
from quimera.runtime.mcp.settings import (
    SCOPE_GLOBAL,
    SCOPE_LOCAL,
    MCPConnectionEntry,
    MCPConnectionStore,
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
    #: Onde a spec em vigor está gravada: ``global`` (catálogo) ou ``local``.
    scope: str = SCOPE_LOCAL
    #: Entra nesta sessão. Locais começam ativas e podem ser desligadas aqui;
    #: as globais seguem o padrão global, a menos que o workspace tenha uma exceção.
    active: bool = True
    #: Quem decidiu ``active``: ``global`` (o padrão) ou ``local`` (este workspace).
    activation_scope: str = SCOPE_LOCAL
    #: Padrão global de ativação da conexão, para explicar uma exceção local.
    default_active: bool = False

    @property
    def is_global(self) -> bool:
        return self.scope == SCOPE_GLOBAL

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
        if not self.active:
            return "inativo"
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
    scope: str = SCOPE_LOCAL,
    active: bool = True,
    activation_scope: str = SCOPE_LOCAL,
    default_active: bool = False,
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
        scope=scope,
        active=active,
        activation_scope=activation_scope,
        default_active=default_active,
    )


logger = logging.getLogger(__name__)


class MCPConnectionManager:
    """Fonte única para persistência e estado vivo dos MCP clients externos.

    A persistência é o :class:`MCPConnectionStore` do workspace: catálogo
    global com ativação por workspace e conexões locais. O estado vivo é o
    bridge compartilhado da sessão.
    """

    def __init__(
        self,
        *,
        executor: Any,
        workspace,
        store: MCPConnectionStore | None = None,
    ) -> None:
        self.executor = executor
        self.workspace = workspace
        self.store = store if store is not None else MCPConnectionStore.from_workspace(workspace)
        bridge = get_bridge()
        if bridge is None:
            bridge = MCPClientBridge()
            set_bridge(bridge)
        self.bridge = bridge

    @classmethod
    def from_app(cls, app: Any) -> "MCPConnectionManager":
        """Cria o manager usando os arquivos MCP do workspace atual."""
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

    def list_connections(self) -> list[MCPConnectionInfo]:
        """Lista o catálogo e as conexões locais com o estado vivo da sessão."""
        return [self._describe(entry) for entry in self.store.entries()]

    def _describe(self, entry: MCPConnectionEntry) -> MCPConnectionInfo:
        state = self.bridge.state(entry.name) if entry.active else None
        return describe_mcp_client_spec(
            entry.spec,
            connected=entry.active and entry.name in self.bridge.sessions,
            state=state,
            scope=entry.scope,
            active=entry.active,
            activation_scope=entry.activation_scope,
            default_active=entry.default_active,
        )

    def subscribe(self, listener) -> Any:
        """Observa mudanças de estado das conexões; retorna o cancelamento."""
        return self.bridge.subscribe(listener)

    def set_active(
        self,
        name: str,
        active: bool,
        scope: str = SCOPE_LOCAL,
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> MCPConnectionInfo:
        """Liga ou desliga uma conexão e aplica na sessão.

        ``scope`` diz onde a decisão vale: neste workspace (``local``) ou como
        padrão de todos (``global``, o que este workspace também passa a
        seguir). Ativar persiste e conecta em background; desativar derruba a
        sessão e esquece o estado. Conexões locais só aceitam o escopo local
        (``ValueError`` no global: não têm padrão global).
        """
        entry = self.store.set_active(name, active, scope)
        if active:
            self.bridge.mark_pending(name, spec_transport_type(entry.spec))
            self._start_connection_thread(entry.spec, thread_factory=thread_factory)
        else:
            self.bridge.disconnect_connection(name, forget=True)
            refresh_registration(self.executor, self.bridge)
        return self._describe(entry)

    def reconnect(self, name: str) -> MCPConnectionInfo:
        """Refaz o handshake da conexão persistida sem alterar configuração.

        Bloqueia até o handshake terminar; a UI usa
        :meth:`reconnect_in_background`, que não segura a tela enquanto o
        servidor espera (ex.: autorização OAuth no navegador).
        """
        entry = self._require_active(name)
        parsed_name, transport = parse_mcp_client_spec(
            entry.spec,
            self._env_overrides(),
            workspace=self.workspace,
        )
        bind_bridge_notices(self.bridge, parsed_name, transport)
        self.bridge.replace_connection(parsed_name, transport)
        refresh_registration(self.executor, self.bridge)
        return self._describe(entry)

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
        :meth:`subscribe`. Levanta ``KeyError`` se a conexão não existe e
        ``ValueError`` se está inativa neste workspace.
        """
        entry = self._require_active(name)
        if entry.name not in self.bridge.sessions:
            # Feedback imediato na UI; com sessão viva o estado segue
            # "connected" até a troca transacional terminar.
            self.bridge.mark_connecting(entry.name, spec_transport_type(entry.spec))
        return self._start_connection_thread(entry.spec, thread_factory=thread_factory)

    def upsert_in_background(
        self,
        spec: str,
        *,
        env_spec: str | None = None,
        scope: str = SCOPE_LOCAL,
        previous_name: str | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> MCPConnectionInfo:
        """Valida e persiste a configuração no escopo pedido e a aplica à sessão.

        Salvar não liga nem desliga: a decisão de ativação deste workspace é
        preservada pelo store (ver :meth:`MCPConnectionStore.upsert`) e só
        :meth:`set_active` a muda. Se a conexão está ativa aqui e a spec ou o
        ambiente mudaram, o handshake refaz a sessão em background com a
        configuração nova; inativa, fica só gravada. A configuração é gravada
        antes do handshake: uma conexão que exige autorização OAuth não pode
        segurar o editor até o usuário concluir no navegador. Specs inválidas
        levantam ``ValueError`` antes de qualquer persistência.
        ``previous_name`` é o nome anterior de uma conexão renomeada: a sessão
        antiga é derrubada e a ativação passa ao nome novo.
        """
        name = _spec_name(spec)
        if not name:
            raise ValueError("Conexão MCP exige um nome")
        if env_spec is None:
            existing = self.store.entry(previous_name or name)
            env_specs = [existing.env_spec] if existing is not None and existing.env_spec else None
        else:
            env_specs = [env_spec] if env_spec.strip() else None
        _parsed_name, transport = parse_mcp_client_spec(
            spec,
            parse_mcp_client_env_specs(env_specs),
            workspace=self.workspace,
        )
        before = self.store.entry(name)
        entry = self.store.upsert(spec, env_spec=env_spec, scope=scope, replaces=previous_name)
        if previous_name and previous_name != entry.name:
            self.bridge.disconnect_connection(previous_name, forget=True)
        changed = before is None or (before.spec, before.env_spec) != (entry.spec, entry.env_spec)
        if entry.active and changed:
            self.bridge.mark_pending(entry.name, transport.transport_type)
            self._start_connection_thread(entry.spec, thread_factory=thread_factory)
        elif not entry.active:
            # Inativa aqui: nenhuma sessão fica viva com a configuração antiga.
            self.bridge.disconnect_connection(entry.name, forget=True)
            refresh_registration(self.executor, self.bridge)
        elif previous_name and previous_name != entry.name:
            refresh_registration(self.executor, self.bridge)
        return self._describe(entry)

    def _start_connection_thread(
        self,
        spec: str,
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> threading.Thread:
        name = _spec_name(spec)
        thread = thread_factory(
            target=connect_mcp_client_spec,
            args=(self.bridge, spec),
            kwargs={
                "env_overrides": self._env_overrides(),
                "workspace": self.workspace,
                "executor": self.executor,
                "expected_attempt": self.bridge.prepare_connection(name),
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

    def remove(self, name: str) -> MCPConnectionInfo | None:
        """Desconecta e remove a configuração do escopo em que ela vive.

        Uma global some do catálogo, logo de todos os workspaces. Uma local
        some deste workspace; se o catálogo tiver uma de mesmo nome e ela
        estiver ativa aqui, volta a valer e é reconectada em background.
        Devolve a entrada que passou a valer, ou ``None``.
        """
        self.bridge.disconnect_connection(name, forget=True)
        refresh_registration(self.executor, self.bridge)
        fallback = self.store.remove(name)
        if fallback is None:
            return None
        if fallback.active:
            self.bridge.mark_pending(fallback.name, spec_transport_type(fallback.spec))
            self._start_connection_thread(fallback.spec)
        return self._describe(fallback)

    def env_text_for(self, name: str) -> str:
        """Retorna os pares ``KEY=valor`` em vigor para a conexão (vazio se não há)."""
        entry = self.store.entry(name)
        return entry.env_text if entry is not None else ""

    def _env_overrides(self) -> dict[str, dict[str, str]] | None:
        return parse_mcp_client_env_specs(self.store.active_specs()[1])

    def _require_active(self, name: str) -> MCPConnectionEntry:
        entry = self.store.entry(name)
        if entry is None:
            raise KeyError(f"Conexão MCP não configurada: {name}")
        if not entry.active:
            raise ValueError(f"A conexão '{name}' está inativa neste workspace; ative-a primeiro.")
        return entry
