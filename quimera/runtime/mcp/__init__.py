"""Pacote MCP: servidor JSON-RPC 2.0 sobre stdio, socket Unix e HTTP+SSE."""

from quimera.runtime.mcp.server import MCPServer
from quimera.runtime.mcp.server import main as mcp_server_main
from quimera.runtime.mcp.http_server import MCP_HTTPServer
from quimera.runtime.mcp.http_server import create_server
from quimera.runtime.mcp.oauth import (
    AuthContext,
    OAuthClient,
    OAuthConfig,
    OAuthError,
    OAuthProvider,
    OAuthRedirectError,
    build_provider_from_cli,
    parse_client_specs,
)
from quimera.runtime.mcp.session import (
    EmbeddedMCPRuntime,
    build_oauth_provider,
    start_embedded_mcp,
)
from quimera.runtime.mcp.client import (
    MCPClientBridge,
    MCPClientSession,
    MCPTransport,
    StdioMCPTransport,
    SocketMCPTransport,
    HttpMCPTransport,
    build_bridge_from_cli,
    parse_mcp_client_spec,
)

__all__ = [
    "MCPServer",
    "mcp_server_main",
    "MCP_HTTPServer",
    "create_server",
    "EmbeddedMCPRuntime",
    "start_embedded_mcp",
    "build_oauth_provider",
    "AuthContext",
    "OAuthClient",
    "OAuthConfig",
    "OAuthError",
    "OAuthProvider",
    "OAuthRedirectError",
    "build_provider_from_cli",
    "parse_client_specs",
    "MCPClientBridge",
    "MCPClientSession",
    "MCPTransport",
    "StdioMCPTransport",
    "SocketMCPTransport",
    "HttpMCPTransport",
    "build_bridge_from_cli",
    "parse_mcp_client_spec",
]
