"""Pacote MCP: servidor JSON-RPC 2.0 sobre stdio, socket Unix e HTTP+SSE."""

from quimera.runtime.mcp.server import MCPServer
from quimera.runtime.mcp.server import main as mcp_server_main
from quimera.runtime.mcp.http_server import MCP_HTTPServer
from quimera.runtime.mcp.http_server import create_server

__all__ = [
    "MCPServer",
    "mcp_server_main",
    "MCP_HTTPServer",
    "create_server",
]
