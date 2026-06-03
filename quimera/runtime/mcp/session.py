"""Inicialização do servidor MCP embutido na sessão do Quimera."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from quimera.runtime.mcp.http_server import MCP_HTTPServer
from quimera.runtime.mcp.server import MCPServer

MCPTransport = Literal["socket", "http"]


@dataclass(frozen=True)
class EmbeddedMCPRuntime:
    """Estado retornado pela inicialização do MCP embutido."""

    enabled: bool
    transport: MCPTransport | None = None
    token: str | None = None
    mcp_server: MCPServer | None = None
    http_server: MCP_HTTPServer | None = None
    socket_path: str | None = None
    http_url: str | None = None


def _prompt_session_state(app: Any) -> dict | None:
    prompt_builder = getattr(app, "prompt_builder", None)
    session_state = getattr(prompt_builder, "session_state", None)
    return session_state if isinstance(session_state, dict) else None


def _resolve_token(token_env: str | None) -> str:
    env_name = token_env or ""
    return (os.environ.get(env_name) or "").strip() or secrets.token_urlsafe(32)


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
) -> EmbeddedMCPRuntime:
    """Inicia e propaga o MCP embutido para a sessão do Quimera.

    A função centraliza a lógica de startup do MCP para manter a CLI apenas
    como camada de parsing/validação de argumentos.

    Args:
        app: Instância de QuimeraApp, ou objeto compatível, com ``tool_executor``
            e métodos ``configure_mcp_socket``/``configure_mcp_http``.
        workspace: Workspace atual, usado para gerar o socket temporário padrão.
        enabled: Quando falso, desativa MCP na aplicação e não inicia servidor.
        transport: ``"socket"`` para Unix socket ou ``"http"`` para Streamable HTTP.
        socket_path: Path opcional do socket Unix. Se omitido no transporte socket,
            um path temporário é gerado no workspace.
        http_host: Host de bind do MCP HTTP.
        http_port: Porta de bind do MCP HTTP.
        token_env: Nome da variável de ambiente com token fixo de autenticação.
            Se ausente ou vazia, gera token aleatório por sessão.

    Returns:
        Estado do runtime MCP iniciado, incluindo servidor, token e endpoint.
    """
    session_state = _prompt_session_state(app)

    if not enabled:
        app.configure_mcp_socket(None)
        if isinstance(session_state, dict):
            session_state["mcp_enabled"] = False
            session_state["mcp_socket_path"] = ""
            session_state["mcp_http_url"] = ""
        setattr(app, "mcp_socket_path", None)
        setattr(app, "mcp_http_url", None)
        return EmbeddedMCPRuntime(enabled=False)

    if transport not in {"socket", "http"}:
        raise ValueError(f"Transporte MCP inválido: {transport!r}")

    mcp_token = _resolve_token(token_env)
    mcp = MCPServer(app.tool_executor, auth_token=mcp_token)

    if transport == "http":
        http_server = MCP_HTTPServer(mcp, host=http_host, port=http_port)
        http_server.start_background()
        http_url = f"http://{http_host}:{http_port}/mcp"
        app.configure_mcp_http(http_url, mcp_token)
        setattr(app, "mcp_http_url", http_url)
        setattr(app, "mcp_socket_path", None)
        if isinstance(session_state, dict):
            session_state["mcp_enabled"] = True
            session_state["mcp_socket_path"] = ""
            session_state["mcp_http_url"] = http_url
        return EmbeddedMCPRuntime(
            enabled=True,
            transport="http",
            token=mcp_token,
            mcp_server=mcp,
            http_server=http_server,
            http_url=http_url,
        )

    resolved_socket_path = socket_path or _default_socket_path(workspace)
    mcp.start_background(resolved_socket_path)
    app.configure_mcp_socket(resolved_socket_path, mcp_token)
    setattr(app, "mcp_socket_path", resolved_socket_path)
    setattr(app, "mcp_http_url", None)
    if isinstance(session_state, dict):
        session_state["mcp_enabled"] = True
        session_state["mcp_socket_path"] = resolved_socket_path
        session_state["mcp_http_url"] = ""
    return EmbeddedMCPRuntime(
        enabled=True,
        transport="socket",
        token=mcp_token,
        mcp_server=mcp,
        socket_path=resolved_socket_path,
    )
