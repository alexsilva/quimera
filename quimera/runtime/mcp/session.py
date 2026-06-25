"""Inicialização do servidor MCP embutido na sessão do Quimera."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from quimera.runtime.mcp.http_server import (
    DEFAULT_HTTP_READ_ONLY_TOOLS,
    DEFAULT_HTTP_TOOL_PROFILE,
    HTTP_TOOL_PROFILES,
    MCP_HTTPServer,
)
from quimera.runtime.mcp.server import MCPServer

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
    external_mcp_token: str | None = None
    external_mcp_allowed_tools: frozenset[str] | None = None
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


def _resolve_external_token(token_env: str | None) -> str:
    env_name = token_env or ""
    return (os.environ.get(env_name) or "").strip() or secrets.token_urlsafe(32)


def _resolve_internal_token() -> str:
    return secrets.token_urlsafe(32)


def parse_http_allowed_tools(value: str | Iterable[str] | None) -> frozenset[str] | None:
    """Normaliza a configuração de allowlist do MCP HTTP.

    ``None`` ou ``"read"`` usa o perfil padrão de leitura com web.
    Perfis disponíveis: ``"read-local"`` (sem rede), ``"read"`` (com web),
    ``"agent"`` (leitura com web + ``delegate``) e ``"all"`` (sem filtro).
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
        items = value.split(",")
    else:
        items = value
    tools = frozenset(str(item).strip() for item in items if str(item).strip())
    return tools or DEFAULT_HTTP_READ_ONLY_TOOLS


def _default_socket_path(workspace: Any) -> str:
    rand_suffix = secrets.token_hex(8)
    return str(workspace.tmp.root / f"mcp-{rand_suffix}.sock")


def start_embedded_mcp(
    app: Any,
    workspace: Any,
    *,
    enabled: bool = True,
    transport: MCPTransport = "socket",
    socket_path: str | None = None,
    http_host: str = "127.0.0.1",
    http_port: int = 9090,
    token_env: str | None = "QUIMERA_MCP_TOKEN",
    http_allowed_tools: str | Iterable[str] | None = DEFAULT_HTTP_TOOL_PROFILE,
    external_http_enabled: bool = False,
) -> EmbeddedMCPRuntime:
    """Inicia o MCP interno obrigatório e, opcionalmente, o MCP HTTP externo.

    O socket Unix interno é o canal principal para agentes locais e sempre expõe
    todas as ferramentas registradas no ``ToolExecutor``. O HTTP externo é uma
    instância separada de ``MCPServer`` para clientes remotos, com allowlist
    aplicada somente nessa instância.
    """
    session_state = _prompt_session_state(app)

    if not enabled:
        app.configure_mcp_socket(None)
        configure_http = getattr(app, "configure_mcp_http", None)
        if callable(configure_http):
            configure_http(None)
        if isinstance(session_state, dict):
            session_state["mcp_enabled"] = False
            session_state["mcp_socket_path"] = ""
            session_state["mcp_http_url"] = ""
            session_state["mcp_internal_socket_path"] = ""
            session_state["mcp_external_http_url"] = ""
        setattr(app, "mcp_socket_path", None)
        setattr(app, "mcp_http_url", None)
        setattr(app, "internal_mcp_socket_path", None)
        setattr(app, "external_mcp_http_url", None)
        return EmbeddedMCPRuntime(enabled=False)

    if transport not in {"socket", "http"}:
        raise ValueError(f"Transporte MCP inválido: {transport!r}")
    external_http_enabled = external_http_enabled or transport == "http"

    internal_mcp_token = _resolve_internal_token()
    internal_mcp_server = MCPServer(app.tool_executor, auth_token=internal_mcp_token)
    resolved_socket_path = socket_path or _default_socket_path(workspace)
    internal_mcp_server.start_background(resolved_socket_path)
    app.configure_mcp_socket(resolved_socket_path, internal_mcp_token)
    setattr(app, "mcp_socket_path", resolved_socket_path)
    setattr(app, "internal_mcp_socket_path", resolved_socket_path)
    setattr(app, "internal_mcp_server", internal_mcp_server)

    external_mcp_server = None
    external_mcp_http_server = None
    external_mcp_http_url = None
    external_mcp_token = None
    external_mcp_allowed_tools = None

    if external_http_enabled:
        external_mcp_token = _resolve_external_token(token_env)
        external_mcp_server = MCPServer(app.tool_executor, auth_token=external_mcp_token)
        external_mcp_allowed_tools = parse_http_allowed_tools(http_allowed_tools)
        external_mcp_http_server = MCP_HTTPServer(
            external_mcp_server,
            host=http_host,
            port=http_port,
            allowed_tools=external_mcp_allowed_tools,
        )
        external_mcp_http_server.start_background()
        external_mcp_http_url = f"http://{http_host}:{http_port}/mcp"
        # Intencionalmente não propagamos HTTP aos profiles locais: eles devem
        # preferir o socket interno mesmo quando o HTTP externo está ativo.
        setattr(app, "mcp_http_url", external_mcp_http_url)
        setattr(app, "external_mcp_http_url", external_mcp_http_url)
    else:
        setattr(app, "mcp_http_url", None)
        setattr(app, "external_mcp_http_url", None)

    if isinstance(session_state, dict):
        session_state["mcp_enabled"] = True
        session_state["mcp_socket_path"] = resolved_socket_path
        session_state["mcp_http_url"] = external_mcp_http_url or ""
        session_state["mcp_internal_socket_path"] = resolved_socket_path
        session_state["mcp_external_http_url"] = external_mcp_http_url or ""

    return EmbeddedMCPRuntime(
        enabled=True,
        internal_mcp_server=internal_mcp_server,
        internal_mcp_socket_path=resolved_socket_path,
        internal_mcp_token=internal_mcp_token,
        external_mcp_server=external_mcp_server,
        external_mcp_http_server=external_mcp_http_server,
        external_mcp_http_url=external_mcp_http_url,
        external_mcp_token=external_mcp_token,
        external_mcp_allowed_tools=external_mcp_allowed_tools,
        transport="socket",
        token=internal_mcp_token,
        mcp_server=internal_mcp_server,
        http_server=external_mcp_http_server,
        socket_path=resolved_socket_path,
        http_url=external_mcp_http_url,
        allowed_tools=external_mcp_allowed_tools,
    )
