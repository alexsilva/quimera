"""Pacote MCP: servidor JSON-RPC 2.0 sobre stdio, socket Unix e HTTP+SSE."""

from quimera.runtime.mcp.server import MCPServer
from quimera.runtime.mcp.server import _openai_schema_to_mcp
from quimera.runtime.mcp.server import _proxy_stdio_to_socket
from quimera.runtime.mcp.server import main as mcp_server_main
from quimera.runtime.mcp.http_server import MCP_HTTPServer
from quimera.runtime.mcp.http_server import _MCPHTTPRequestHandler
from quimera.runtime.mcp.http_server import _SSEQueueOutput
from quimera.runtime.mcp.http_server import create_server

__all__ = [
    "MCPServer",
    "_openai_schema_to_mcp",
    "_proxy_stdio_to_socket",
    "mcp_server_main",
    "MCP_HTTPServer",
    "_MCPHTTPRequestHandler",
    "_SSEQueueOutput",
    "create_server",
]
