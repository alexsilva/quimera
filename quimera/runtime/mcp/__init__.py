"""Pacote MCP: servidor JSON-RPC 2.0 sobre stdio, socket Unix e HTTP+SSE."""

from quimera.runtime.mcp.server import MCPServer
from quimera.runtime.mcp.server import main as mcp_server_main
from quimera.runtime.mcp.http_server import MCP_HTTPServer
from quimera.runtime.mcp.http_server import create_server
from quimera.runtime.mcp.session import EmbeddedMCPRuntime, start_embedded_mcp
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
    "MCPClientBridge",
    "MCPClientSession",
    "MCPTransport",
    "StdioMCPTransport",
    "SocketMCPTransport",
    "HttpMCPTransport",
    "build_bridge_from_cli",
    "parse_mcp_client_spec",
]
