"""Inicialização do servidor MCP embutido na sessão do Quimera."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from quimera.environment import RuntimeSecrets
from quimera.runtime.mcp.http_server import (
    ConnectedMCPClient,
    DEFAULT_HTTP_READ_ONLY_TOOLS,
    DEFAULT_HTTP_TOOL_PROFILE,
    HTTP_TOOL_PROFILES,
    MCP_HTTPServer,
)
from quimera.runtime.mcp.oauth import (
    OAuthConfig,
    OAuthProvider,
    OAuthStore,
    build_provider_from_cli,
)
from quimera.runtime.mcp.server import MCPServer
from quimera.runtime.mcp.settings import (
    DEFAULT_MCP_HTTP_ALLOW_TOOLS,
    DEFAULT_MCP_HTTP_HOST,
    DEFAULT_MCP_HTTP_PORT,
    MCPHttpSettings,
    MCPHttpSettingsStore,
)

_logger = logging.getLogger(__name__)
_external_http_lock = threading.RLock()

MCPTransport = Literal["socket", "http"]


@dataclass(frozen=True)
class EmbeddedMCPRuntime:
    """Estado retornado pela inicialização do MCP embutido."""

    enabled: bool
    internal_mcp_server: MCPServer | None = None
    internal_mcp_socket_path: str | None = None
    internal_mcp_token: str | None = None
    external_mcp_server: MCPServer | None = None
    external_mcp_http_server: MCP_HTTPServer | None = None
    external_mcp_http_url: str | None = None
    external_mcp_allowed_tools: frozenset[str] | None = None
    external_mcp_oauth: OAuthProvider | None = None
    transport: MCPTransport | None = None
    token: str | None = None
    mcp_server: MCPServer | None = None
    http_server: MCP_HTTPServer | None = None
    socket_path: str | None = None
    http_url: str | None = None
    allowed_tools: frozenset[str] | None = None


def _prompt_session_state(app: Any) -> dict | None:
    prompt_builder = getattr(app, "prompt_builder", None)
    session_state = getattr(prompt_builder, "session_state", None)
    return session_state if isinstance(session_state, dict) else None


def _resolve_internal_token() -> str:
    # token_urlsafe pode começar com '-', que clientes CLI interpretam como
    # flag ao repassar o token em linha de comando. Regenera até evitar isso.
    token = secrets.token_urlsafe(32)
    while token.startswith("-"):
        token = secrets.token_urlsafe(32)
    return token


def parse_http_allowed_tools(value: str | Iterable[str] | None) -> frozenset[str] | None:
    """Normaliza a configuração de allowlist do MCP HTTP.

    ``None`` ou ``"read"`` usa o perfil padrão de leitura com web.
    Perfis disponíveis: ``"read-local"`` (sem rede), ``"read"`` (com web),
    ``"agent"`` (leitura com web + ``tasks_delegate``) e ``"all"`` (sem filtro).
    Strings CSV e iteráveis viram allowlist explícita.
    """
    if value is None:
        return DEFAULT_HTTP_READ_ONLY_TOOLS
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return DEFAULT_HTTP_READ_ONLY_TOOLS
        if normalized in HTTP_TOOL_PROFILES:
            return HTTP_TOOL_PROFILES[normalized]
        items: Iterable[str] = value.split(",")
    else:
        items = value
    tools = frozenset(str(item).strip() for item in items if str(item).strip())
    return tools or DEFAULT_HTTP_READ_ONLY_TOOLS


def _default_socket_path(session_paths: Any) -> str:
    rand_suffix = secrets.token_hex(8)
    return str(session_paths.mcp_socket_path(rand_suffix))


def _default_oauth_store_path(workspace: Any) -> Path | None:
    """Resolve o estado do servidor MCP via ``workspace.mcp_server_file``.

    O caminho em si (``<base_dir>/state/mcp-server.json``) é responsabilidade da
    classe ``Workspace``, que já centraliza os demais paths do app. Retorna
    ``None`` quando o workspace informado não expõe essa property (ex.: mocks
    de teste sem ``base_dir``).
    """
    return getattr(workspace, "mcp_server_file", None)


def build_oauth_provider(
    workspace: Any,
    *,
    issuer: str | None = None,
    client_specs: Iterable[str] | None = None,
    redirect_uris: Iterable[str] | None = None,
    passcode_env: str | None = None,
    auto_approve: bool | None = None,
    allow_dynamic_registration: bool | None = None,
    store_path: str | None = None,
) -> OAuthProvider:
    """Constrói o ``OAuthProvider`` do MCP HTTP externo.

    O store padrão fica em ``<base_dir>/state/mcp-server.json`` (global do app),
    de modo que clients registrados dinamicamente e refresh tokens sobrevivam a
    reinícios de sessão e a troca de workspace sem exigir reautorização.
    """
    return build_provider_from_cli(
        enabled=True,
        issuer=issuer,
        client_specs=client_specs,
        redirect_uris=redirect_uris,
        passcode_env=passcode_env,
        auto_approve=auto_approve,
        allow_dynamic_registration=allow_dynamic_registration,
        store_path=store_path or _default_oauth_store_path(workspace),
        runtime_secrets=RuntimeSecrets(workspace),
    )


def load_persisted_authorized_clients(workspace: Any) -> list[ConnectedMCPClient]:
    """Lê autorizações OAuth válidas sem iniciar nem alterar o servidor MCP.

    A tela do Hub também existe em sessões iniciadas sem ``--mcp-http``. Nessa
    situação não há um ``MCP_HTTPServer`` em memória, mas o estado global ainda
    deve continuar visível. A leitura usa diretamente o store para evitar criar
    um ``OAuthProvider`` concorrente, cuja compactação/persistência poderia
    sobrescrever mudanças feitas pela instância que realmente serve o HTTP.
    """
    store_path = _default_oauth_store_path(workspace)
    if store_path is None:
        return []

    runtime_secrets = RuntimeSecrets(workspace)
    config = OAuthConfig.from_env(
        runtime_secrets=runtime_secrets,
        store_path=store_path,
    )
    clients, access_tokens, refresh_tokens = OAuthStore(
        config.store_path,
        config.store_key,
    ).load_state()

    token_by_client: dict[str, Any] = {}
    for token in (*access_tokens.values(), *refresh_tokens.values()):
        client_id = str(token.client_id or "").strip()
        if client_id:
            token_by_client.setdefault(client_id, token)

    result: list[ConnectedMCPClient] = []
    seen_client_ids: set[str] = set()
    for client_id, client in clients.items():
        client_token = token_by_client.get(client_id)
        if client_token is None:
            continue
        seen_client_ids.add(client_id)
        result.append(
            ConnectedMCPClient(
                session_id="",
                client_id=client_id,
                client_name=str(client.client_name or client_id),
                scope=str(client.scope or client_token.scope or "mcp"),
                profile="",
                initialized=False,
                connected=False,
                authorized=True,
            )
        )
    for client_id, token in token_by_client.items():
        if client_id in seen_client_ids:
            continue
        result.append(
            ConnectedMCPClient(
                session_id="",
                client_id=client_id,
                client_name=client_id,
                scope=str(token.scope or "mcp"),
                profile="",
                initialized=False,
                connected=False,
                authorized=True,
            )
        )
    return result


def _publish_external_http(
    app: Any,
    http_server: MCP_HTTPServer | None,
    url: str | None,
    *,
    error: str = "",
) -> None:
    """Publica (ou limpa) o MCP HTTP externo nos atributos do app e no estado da sessão.

    Intencionalmente não propaga HTTP aos profiles locais: eles devem
    preferir o socket interno mesmo quando o HTTP externo está ativo.
    """
    setattr(app, "mcp_http_url", url)
    setattr(app, "external_mcp_http_url", url)
    setattr(app, "external_mcp_http_server", http_server)
    setattr(app, "external_mcp_http_error", error)
    if http_server is None:
        setattr(app, "mcp_http_settings", None)
    session_state = _prompt_session_state(app)
    if isinstance(session_state, dict):
        session_state["mcp_http_url"] = url or ""
        session_state["mcp_external_http_url"] = url or ""
        session_state["mcp_external_oauth_enabled"] = bool(
            http_server is not None and http_server.oauth.enabled
        )


def workspace_client_access(workspace: Any) -> Callable[[str], bool] | None:
    """Filtro de clients OAuth do servidor HTTP: só os liberados no workspace entram.

    Lê o overlay ``mcp-server.json`` do workspace a cada consulta, então uma
    mudança feita no Hub vale na requisição seguinte, sem reiniciar o servidor.
    Workspaces mínimos de testes, sem esse arquivo, não têm filtro.
    """
    try:
        store = MCPHttpSettingsStore.from_workspace(workspace)
    except AttributeError:
        return None
    return store.is_client_active


def describe_http_error(exc: BaseException) -> str:
    """Resumo de uma falha de partida do HTTP com a causa (ex.: porta em uso)."""
    cause = exc.__cause__ or exc.__context__
    text = str(exc) or type(exc).__name__
    if cause is not None and str(cause):
        text = f"{text}: {cause}"
    return text


def start_external_mcp_http(
    app: Any,
    workspace: Any,
    settings: MCPHttpSettings,
    *,
    oauth: OAuthProvider | None = None,
) -> MCP_HTTPServer:
    """Sobe o MCP HTTP externo na sessão viva e publica a URL no app.

    Usado no boot e pelo MCP Hub ao ativar o servidor no workspace. É uma
    instância separada de ``MCPServer`` para clientes remotos, com allowlist
    aplicada só nela e OAuth obrigatório (o token do socket interno não vale
    aqui). Levanta ``RuntimeError`` se já há um servidor ativo ou se o bind
    falha (porta ocupada por outra instância, por exemplo); o erro fica
    publicado para o Hub permitir corrigir a configuração.
    """
    with _external_http_lock:
        if getattr(app, "_mcp_closing", False):
            raise RuntimeError("A sessão está encerrando; o servidor MCP HTTP não pode ser iniciado.")
        if getattr(app, "external_mcp_http_server", None) is not None:
            raise RuntimeError("MCP HTTP externo já está ativo nesta sessão")
        external_mcp_server = None
        http_server = None
        try:
            if oauth is None:
                oauth = build_oauth_provider(workspace, **(getattr(app, "mcp_oauth_options", None) or {}))
            external_mcp_server = MCPServer(
                app.tool_executor,
                auth_token=None,
                agent_run_sink=getattr(app, "agent_run_sink", None),
            )
            http_server = MCP_HTTPServer(
                external_mcp_server,
                host=settings.host,
                port=settings.port,
                allowed_tools=parse_http_allowed_tools(settings.allow_tools),
                oauth=oauth,
                client_access=workspace_client_access(workspace),
            )
            http_server.start_background()
        except Exception as exc:
            try:
                if http_server is not None:
                    http_server.shutdown()
                elif external_mcp_server is not None:
                    external_mcp_server.shutdown()
            except Exception:
                _logger.warning("Falha ao liberar servidor MCP HTTP após erro de partida", exc_info=True)
            _publish_external_http(app, None, None, error=describe_http_error(exc))
            raise
        # port=0 é útil para sessões/testes efêmeros; publique a porta que o
        # sistema de fato reservou. Configurações persistidas usam 1..65535.
        actual_port = http_server.port
        if type(actual_port) is int:
            settings = replace(settings, port=actual_port)
        setattr(app, "mcp_http_settings", settings)
        _publish_external_http(app, http_server, settings.url)
        return http_server


def stop_external_mcp_http(app: Any) -> bool:
    """Derruba o MCP HTTP externo da sessão; ``False`` se não havia servidor."""
    with _external_http_lock:
        http_server = getattr(app, "external_mcp_http_server", None)
        if http_server is None:
            _publish_external_http(app, None, None)
            return False
        http_server.shutdown()
        _publish_external_http(app, None, None)
        return True


def start_embedded_mcp(
    app: Any,
    workspace: Any,
    session_paths: Any,
    *,
    enabled: bool = True,
    transport: MCPTransport = "socket",
    socket_path: str | None = None,
    http_host: str = DEFAULT_MCP_HTTP_HOST,
    http_port: int = DEFAULT_MCP_HTTP_PORT,
    http_allowed_tools: str | None = DEFAULT_HTTP_TOOL_PROFILE,
    external_http_enabled: bool = False,
    oauth: OAuthProvider | None = None,
    oauth_options: dict | None = None,
) -> EmbeddedMCPRuntime:
    """Inicia o MCP interno obrigatório e, opcionalmente, o MCP HTTP externo.

    O socket Unix interno é o canal principal para agentes locais e sempre expõe
    todas as ferramentas registradas no ``ToolExecutor``. O HTTP externo é uma
    instância separada de ``MCPServer`` para clientes remotos, com allowlist
    aplicada somente nessa instância.

    O transporte HTTP externo usa OAuth exclusivamente. O token do socket interno
    não é reutilizado nem aceito pelo HTTP. ``oauth_options`` (flags da CLI)
    ficam guardadas em ``app.mcp_oauth_options`` para que o MCP Hub possa subir
    o servidor mais tarde com o mesmo provider.

    Uma falha ao subir o HTTP (porta ocupada) não derruba a sessão: fica em
    ``app.external_mcp_http_error``, aparece no bloco de status do boot e o
    Hub permite ajustar a porta e ativar de novo.
    """
    session_state = _prompt_session_state(app)
    setattr(app, "mcp_oauth_options", dict(oauth_options or {}))

    if not enabled:
        app.configure_mcp_socket(None)
        configure_http = getattr(app, "configure_mcp_http", None)
        if callable(configure_http):
            configure_http(None)
        if isinstance(session_state, dict):
            session_state["mcp_enabled"] = False
            session_state["mcp_socket_path"] = ""
            session_state["mcp_internal_socket_path"] = ""
        setattr(app, "mcp_socket_path", None)
        setattr(app, "internal_mcp_socket_path", None)
        _publish_external_http(app, None, None)
        return EmbeddedMCPRuntime(enabled=False)

    if transport not in {"socket", "http"}:
        raise ValueError(f"Transporte MCP inválido: {transport!r}")
    external_http_enabled = external_http_enabled or transport == "http"
    agent_run_sink = getattr(app, "agent_run_sink", None)

    internal_mcp_token = _resolve_internal_token()
    internal_mcp_server = MCPServer(
        app.tool_executor,
        auth_token=internal_mcp_token,
        agent_run_sink=agent_run_sink,
    )
    resolved_socket_path = socket_path or _default_socket_path(session_paths)
    internal_mcp_server.start_background(resolved_socket_path)
    app.configure_mcp_socket(resolved_socket_path, internal_mcp_token)
    setattr(app, "mcp_socket_path", resolved_socket_path)
    setattr(app, "internal_mcp_socket_path", resolved_socket_path)
    setattr(app, "internal_mcp_server", internal_mcp_server)
    if isinstance(session_state, dict):
        session_state["mcp_enabled"] = True
        session_state["mcp_socket_path"] = resolved_socket_path
        session_state["mcp_internal_socket_path"] = resolved_socket_path

    external_mcp_http_server = None
    external_mcp_http_url = None
    settings = MCPHttpSettings(
        enabled=external_http_enabled,
        host=http_host,
        port=http_port,
        allow_tools=http_allowed_tools or DEFAULT_MCP_HTTP_ALLOW_TOOLS,
    )
    if external_http_enabled:
        try:
            external_mcp_http_server = start_external_mcp_http(
                app, workspace, settings, oauth=oauth
            )
            external_mcp_http_url = app.external_mcp_http_url
        except Exception as exc:
            error = describe_http_error(exc)
            _logger.error("MCP HTTP externo não iniciado em %s: %s", settings.url, error)
            _publish_external_http(app, None, None, error=error)
    else:
        _publish_external_http(app, None, None)

    return EmbeddedMCPRuntime(
        enabled=True,
        internal_mcp_server=internal_mcp_server,
        internal_mcp_socket_path=resolved_socket_path,
        internal_mcp_token=internal_mcp_token,
        external_mcp_server=(
            external_mcp_http_server.mcp_server if external_mcp_http_server is not None else None
        ),
        external_mcp_http_server=external_mcp_http_server,
        external_mcp_http_url=external_mcp_http_url,
        external_mcp_allowed_tools=(
            external_mcp_http_server.allowed_tools
            if external_mcp_http_server is not None
            else None
        ),
        external_mcp_oauth=(
            external_mcp_http_server.oauth if external_mcp_http_server is not None else None
        ),
        transport="socket",
        token=internal_mcp_token,
        mcp_server=internal_mcp_server,
        http_server=external_mcp_http_server,
        socket_path=resolved_socket_path,
        http_url=external_mcp_http_url,
        allowed_tools=(
            external_mcp_http_server.allowed_tools
            if external_mcp_http_server is not None
            else None
        ),
    )
