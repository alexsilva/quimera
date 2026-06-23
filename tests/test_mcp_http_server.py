"""Testes para quimera.runtime.mcp.http_server.MCP_HTTPServer."""
from __future__ import annotations

import io
import json
import os
import queue
import socket
import threading
import time
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.mcp import (
    MCP_HTTPServer,
    create_server,
    MCPServer,
)
from quimera.runtime.mcp.http_server import (
    DEFAULT_HTTP_READ_ONLY_TOOLS,
    HTTP_AGENT_TOOLS,
    HTTP_READ_LOCAL_TOOLS,
    _SSEQueueOutput,
)
from quimera.runtime.mcp.session import parse_http_allowed_tools
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(tool_names=None, call_result=None):
    executor = MagicMock()
    names = tool_names or ["read_file", "run_shell"]
    executor.registry.names.return_value = names
    executor.config.db_path = None
    executor.policy.blocked_tools = set()
    if call_result is None:
        call_result = ToolResult(ok=True, tool_name="read_file", content="ok")
    executor.execute.return_value = call_result
    return executor


def _make_mcp_server(executor=None):
    return MCPServer(executor or _make_executor())


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect((host, port))
            s.close()
            return
        except (OSError, ConnectionRefusedError):
            time.sleep(0.05)


class _Response:
    def __init__(self, status: int, headers: dict, data: bytes) -> None:
        self.status = status
        self.headers = headers
        self.data = data

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


def _http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict | None = None,
) -> _Response:
    conn = HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        status = resp.status
        resp_headers = dict(resp.getheaders())
        data = resp.read()
        return _Response(status, resp_headers, data)
    finally:
        conn.close()


def _start_http_server(mcp=None) -> MCP_HTTPServer:
    if mcp is None:
        mcp = _make_mcp_server()
    httpd = MCP_HTTPServer(mcp, host="127.0.0.1", port=0)
    httpd.start_background()
    _wait_for_server(httpd.host, httpd.port)
    return httpd


# ---------------------------------------------------------------------------
# _SSEQueueOutput
# ---------------------------------------------------------------------------

class TestSSEQueueOutput:
    def test_write_parses_json_and_puts_on_queue(self):
        """Verifica que Test write parses json and puts on queue."""
        q = MagicMock()
        out = _SSEQueueOutput(q)
        out.write('{"jsonrpc":"2.0","id":1,"result":{}}\n')
        q.put_nowait.assert_called_once_with(
            {"jsonrpc": "2.0", "id": 1, "result": {}}
        )

    def test_write_ignores_empty_string(self):
        """Verifica que Test write ignores empty string."""
        q = MagicMock()
        out = _SSEQueueOutput(q)
        out.write("\n")
        q.put_nowait.assert_not_called()

    def test_write_ignores_invalid_json(self):
        """Verifica que Test write ignores invalid json."""
        q = MagicMock()
        out = _SSEQueueOutput(q)
        out.write("not json\n")
        q.put_nowait.assert_not_called()

    def test_flush_does_nothing(self):
        """Verifica que Test flush does nothing."""
        out = _SSEQueueOutput(MagicMock())
        out.flush()


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200_with_status_ok(self):
        """Verifica que Test health returns 200 with status ok."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET", "/health"
            )
            assert resp.status == 200
            body = json.loads(resp.data)
            assert body["status"] == "ok"
            assert "server" in body
        finally:
            httpd.shutdown()

    def test_health_has_cors_headers(self):
        """Verifica que Test health has cors headers."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET", "/health"
            )
            assert resp.header("access-control-allow-origin") == "*"
        finally:
            httpd.shutdown()

    def test_health_returns_json_content_type(self):
        """Verifica que Test health returns json content type."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET", "/health"
            )
            ct = resp.header("content-type") or ""
            assert "application/json" in ct
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# GET /sse
# ---------------------------------------------------------------------------

class TestSSE:
    def test_sse_returns_200_with_event_stream_content_type(self):
        """Verifica que Test sse returns 200 with event stream content type."""
        httpd = _start_http_server()
        try:
            conn = HTTPConnection(httpd.host, httpd.port, timeout=5)
            conn.request("GET", "/sse")
            resp = conn.getresponse()
            assert resp.status == 200
            ct = resp.getheader("content-type", "")
            assert "text/event-stream" in ct
            resp.close()
            conn.close()
        finally:
            httpd.shutdown()

    def test_sse_has_cors_headers(self):
        """Verifica que Test sse has cors headers."""
        httpd = _start_http_server()
        try:
            conn = HTTPConnection(httpd.host, httpd.port, timeout=5)
            conn.request("GET", "/sse")
            resp = conn.getresponse()
            assert resp.getheader("access-control-allow-origin") == "*"
            resp.close()
            conn.close()
        finally:
            httpd.shutdown()

    def test_sse_sends_endpoint_event(self):
        """Verifica que Test sse sends endpoint event."""
        httpd = _start_http_server()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((httpd.host, httpd.port))
            s.sendall(b"GET /sse HTTP/1.1\r\nHost: localhost\r\n\r\n")
            chunks = b""
            while True:
                try:
                    data = s.recv(4096)
                    if not data:
                        break
                    chunks += data
                    if b"event: endpoint" in chunks:
                        break
                except socket.timeout:
                    break
            s.close()
            body = chunks.split(b"\r\n\r\n", 1)[-1] if b"\r\n\r\n" in chunks else chunks
            assert b"event: endpoint" in body
            assert b"/message?sessionId=" in body
            assert b"data: http://" not in body
        finally:
            httpd.shutdown()

    def test_sse_sends_keepalive(self):
        """Verifica que Test sse sends keepalive."""
        httpd = _start_http_server()
        try:
            with patch("queue.Queue.get", side_effect=queue.Empty):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((httpd.host, httpd.port))
                s.sendall(b"GET /sse HTTP/1.1\r\nHost: localhost\r\n\r\n")
                chunks = b""
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        data = s.recv(4096)
                        if not data:
                            break
                        chunks += data
                        if b": keepalive" in chunks:
                            break
                    except socket.timeout:
                        break
                s.close()
                assert b": keepalive" in chunks
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# OPTIONS
# ---------------------------------------------------------------------------

class TestOptions:
    def test_options_returns_204_with_cors(self):
        """Verifica que Test options returns 204 with cors."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "OPTIONS", "/health"
            )
            assert resp.status == 204
            assert resp.header("access-control-allow-origin") == "*"
            expose_headers = resp.header("access-control-expose-headers") or ""
            assert "MCP-Session-Id" in expose_headers
            methods = resp.header("access-control-allow-methods") or ""
            assert "GET" in methods
        finally:
            httpd.shutdown()

    @patch.dict(os.environ, {"QUIMERA_MCP_HTTP_CORS_ORIGINS": "https://app.example, https://admin.example"})
    def test_cors_uses_configured_origin_allowlist(self):
        """Verifica que Test cors uses configured origin allowlist."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host,
                httpd.port,
                "OPTIONS",
                "/health",
                headers={"Origin": "https://app.example"},
            )
            assert resp.status == 204
            assert resp.header("access-control-allow-origin") == "https://app.example"
            assert resp.header("vary") == "Origin"
        finally:
            httpd.shutdown()

    @patch.dict(os.environ, {"QUIMERA_MCP_HTTP_CORS_ORIGINS": "https://app.example"})
    def test_cors_omits_unconfigured_origin(self):
        """Verifica que Test cors omits unconfigured origin."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host,
                httpd.port,
                "OPTIONS",
                "/health",
                headers={"Origin": "https://evil.example"},
            )
            assert resp.status == 204
            assert resp.header("access-control-allow-origin") is None
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# POST /message
# ---------------------------------------------------------------------------

class TestMessage:
    def test_ping_returns_pong(self):
        """Verifica que Test ping returns pong."""
        httpd = _start_http_server()
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "ping"
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            result = json.loads(resp.data)
            assert result["id"] == 1
            assert result["result"] == {}
        finally:
            httpd.shutdown()

    def test_initialize_returns_server_info(self):
        """Verifica que Test initialize returns server info."""
        httpd = _start_http_server()
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {},
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            result = json.loads(resp.data)
            assert result["result"]["protocolVersion"] == MCPServer.PROTOCOL_VERSION
        finally:
            httpd.shutdown()

    def test_message_has_cors_headers(self):
        """Verifica que Test message has cors headers."""
        httpd = _start_http_server()
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "ping"
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.header("access-control-allow-origin") == "*"
        finally:
            httpd.shutdown()

    def test_invalid_json_returns_400(self):
        """Verifica que Test invalid json returns 400."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            result = json.loads(resp.data)
            assert result["error"]["code"] == -32700
        finally:
            httpd.shutdown()

    def test_non_dict_body_returns_400(self):
        """Verifica que Test non dict body returns 400."""
        httpd = _start_http_server()
        try:
            body = json.dumps(["not", "a", "dict"]).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            result = json.loads(resp.data)
            assert isinstance(result, list)
            assert len(result) == 3
            for item in result:
                assert item["error"]["code"] == -32600
        finally:
            httpd.shutdown()

    def test_initialized_notification_no_response(self):
        """Verifica que Test initialized notification no response."""
        httpd = _start_http_server()
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 202
            assert resp.data == b""
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------

class TestNotFound:
    def test_unknown_get_returns_404(self):
        """Verifica que Test unknown get returns 404."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET", "/unknown"
            )
            assert resp.status == 404
        finally:
            httpd.shutdown()

    def test_unknown_post_returns_404(self):
        """Verifica que Test unknown post returns 404."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/unknown",
            )
            assert resp.status == 404
        finally:
            httpd.shutdown()

    def test_404_has_cors_headers(self):
        """Verifica que Test 404 has cors headers."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET", "/nonexistent"
            )
            assert resp.header("access-control-allow-origin") == "*"
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# SSE integration
# ---------------------------------------------------------------------------

class TestSSEIntegration:
    def test_message_goes_through_sse_channel(self):
        """Verifica que Test message goes through sse channel."""
        httpd = _start_http_server()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((httpd.host, httpd.port))
            s.sendall(b"GET /sse HTTP/1.1\r\nHost: localhost\r\n\r\n")

            chunks = b""
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks += data
                if b"event: endpoint" in chunks:
                    break

            body_part = chunks.decode("utf-8", errors="replace")
            http_body = body_part.split("\r\n\r\n", 1)[-1]
            endpoint_path = None
            for line in http_body.split("\n"):
                if "data:" in line and "/message?" in line:
                    raw = line.split("data:", 1)[-1].strip()
                    from urllib.parse import urlparse
                    parsed = urlparse(raw)
                    endpoint_path = parsed.path + "?" + parsed.query if parsed.query else parsed.path
                    break

            assert endpoint_path, "endpoint_path not found"

            ping_body = json.dumps({
                "jsonrpc": "2.0", "id": 99, "method": "ping"
            }).encode("utf-8")
            conn = HTTPConnection(httpd.host, httpd.port, timeout=5)
            conn.request(
                "POST", endpoint_path,
                body=ping_body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 202
            resp.read()
            conn.close()

            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks += data
                if b'"id": 99' in chunks or b'"id":99' in chunks:
                    break

            s.close()
            assert b"event: message" in chunks
            assert b'"id": 99' in chunks or b'"id":99' in chunks
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# Env var configuration
# ---------------------------------------------------------------------------

class TestEnvConfig:
    @patch.dict(os.environ, {"QUIMERA_MCP_HTTP_HOST": "0.0.0.0",
                              "QUIMERA_MCP_HTTP_PORT": "9090"})
    def test_env_vars_configure_host_and_port(self):
        """Verifica que Test env vars configure host and port."""
        mcp = _make_mcp_server()
        httpd = MCP_HTTPServer(mcp)
        assert httpd.host == "0.0.0.0"
        assert httpd.port == 9090

    @patch.dict(os.environ, {}, clear=True)
    def test_default_host_and_port(self):
        """Verifica que Test default host and port."""
        mcp = _make_mcp_server()
        httpd = MCP_HTTPServer(mcp)
        assert httpd.host == "127.0.0.1"
        assert httpd.port == 8080

    def test_constructor_args_override_env(self):
        """Verifica que Test constructor args override env."""
        os.environ["QUIMERA_MCP_HTTP_HOST"] = "0.0.0.0"
        os.environ["QUIMERA_MCP_HTTP_PORT"] = "9090"
        try:
            mcp = _make_mcp_server()
            httpd = MCP_HTTPServer(mcp, host="10.0.0.1", port=7070)
            assert httpd.host == "10.0.0.1"
            assert httpd.port == 7070
        finally:
            del os.environ["QUIMERA_MCP_HTTP_HOST"]
            del os.environ["QUIMERA_MCP_HTTP_PORT"]


# ---------------------------------------------------------------------------
# create_server helper
# ---------------------------------------------------------------------------

class TestCreateServer:
    def test_create_server_returns_configured_instance(self):
        """Verifica que Test create server returns configured instance."""
        mcp = _make_mcp_server()
        httpd = create_server(mcp, host="127.0.0.1", port=9999)
        assert isinstance(httpd, MCP_HTTPServer)
        assert httpd.host == "127.0.0.1"
        assert httpd.port == 9999
        assert httpd._mcp is mcp

    def test_create_server_uses_defaults(self):
        """Verifica que Test create server uses defaults."""
        mcp = _make_mcp_server()
        httpd = create_server(mcp)
        assert httpd.host == "127.0.0.1"
        assert httpd.port == 8080


# ---------------------------------------------------------------------------
# tools/call via HTTP
# ---------------------------------------------------------------------------

class TestToolsCallHTTP:
    def test_tools_call_no_session_returns_result_synchronously(self):
        """POST /message com tools/call sem sessionId retorna resultado direto (200)."""
        result = ToolResult(ok=True, tool_name="read_file", content="conteudo http")
        executor = _make_executor(call_result=result)
        httpd = _start_http_server(_make_mcp_server(executor))
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            result_data = json.loads(resp.data)
            assert result_data["id"] == 1
            assert result_data["result"]["isError"] is False
            assert result_data["result"]["content"][0]["text"] == "conteudo http"
        finally:
            httpd.shutdown()


    def test_read_local_profile_excludes_network_tools(self):
        """Verifica que Test read local profile excludes network tools."""
        assert parse_http_allowed_tools("read-local") == HTTP_READ_LOCAL_TOOLS
        assert "inspect_symbols" in HTTP_READ_LOCAL_TOOLS
        assert "web_search" not in HTTP_READ_LOCAL_TOOLS
        assert "web_fetch" not in HTTP_READ_LOCAL_TOOLS

    def test_read_profile_keeps_network_read_tools(self):
        """Verifica que Test read profile keeps network read tools."""
        tools = parse_http_allowed_tools("read")
        assert tools == DEFAULT_HTTP_READ_ONLY_TOOLS
        assert "web_search" in tools
        assert "web_fetch" in tools

    def test_agent_profile_adds_delegate_without_dangerous_write_or_shell_tools(self):
        """Verifica que Test agent profile adds call agent without dangerous write or shell tools."""
        tools = parse_http_allowed_tools("agent")
        assert tools == HTTP_AGENT_TOOLS
        assert "delegate" in tools
        assert "memory_save" in tools
        assert "replace_text" in tools
        for blocked in {"run_shell", "exec_command", "write_file", "remove_file", "apply_patch"}:
            assert blocked not in tools

    def test_all_profile_disables_allowlist(self):
        """Verifica que Test all profile disables allowlist."""
        assert parse_http_allowed_tools("all") is None

    def test_tools_list_uses_default_read_only_allowlist(self):
        """Verifica que Test tools list uses default read only allowlist."""
        executor = _make_executor(tool_names=["read_file", "run_shell", "grep_search"])
        httpd = _start_http_server(_make_mcp_server(executor))
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/list",
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            tool_names = {
                tool["name"] for tool in json.loads(resp.data)["result"]["tools"]
            }
            assert "read_file" in tool_names
            assert "grep_search" in tool_names
            assert "run_shell" not in tool_names
            assert httpd.allowed_tools == DEFAULT_HTTP_READ_ONLY_TOOLS
        finally:
            httpd.shutdown()


    def test_profile_allowlists_filter_http_tools_for_external_clients(self):
        """Verifica que Test profile allowlists filter http tools for external clients."""
        executor = _make_executor(tool_names=[
            "read_file", "grep_search", "inspect_symbols", "web_search", "web_fetch",
            "memory_retrieve", "memory_save", "delegate", "run_shell", "write_file", "apply_patch",
            "replace_text",
        ])
        cases = {
            "read-local": {"read_file", "grep_search", "inspect_symbols", "memory_retrieve"},
            "read": {"read_file", "grep_search", "inspect_symbols", "web_search", "web_fetch", "memory_retrieve"},
            "agent": {"read_file", "grep_search", "inspect_symbols", "web_search", "web_fetch", "memory_retrieve", "memory_save", "delegate", "replace_text"},
        }
        for profile, expected_subset in cases.items():
            httpd = MCP_HTTPServer(
                _make_mcp_server(executor),
                host="127.0.0.1",
                port=0,
                allowed_tools=parse_http_allowed_tools(profile),
            )
            httpd.start_background()
            _wait_for_server(httpd.host, httpd.port)
            try:
                body = json.dumps({
                    "jsonrpc": "2.0", "id": 33, "method": "tools/list",
                }).encode("utf-8")
                resp = _http_request(
                    httpd.host, httpd.port, "POST", "/message",
                    body=body, headers={"Content-Type": "application/json"},
                )
                tool_names = {tool["name"] for tool in json.loads(resp.data)["result"]["tools"]}
                assert expected_subset <= tool_names
                if profile == "read-local":
                    assert "web_search" not in tool_names
                    assert "web_fetch" not in tool_names
                if profile != "agent":
                    assert "delegate" not in tool_names
                    assert "replace_text" not in tool_names
                assert "run_shell" not in tool_names
                assert "write_file" not in tool_names
                assert "apply_patch" not in tool_names
            finally:
                httpd.shutdown()

    def test_internal_mcp_server_lists_sensitive_tools_without_http_allowlist(self):
        """Verifica que Test internal mcp server lists sensitive tools without http allowlist."""
        executor = _make_executor(tool_names=["read_file", "run_shell", "write_file", "apply_patch"])
        server = _make_mcp_server(executor)
        inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 34, "method": "tools/list"}) + "\n")
        out = io.StringIO()

        server.serve(stdin=inp, stdout=out)

        response = json.loads(out.getvalue())
        tool_names = {tool["name"] for tool in response["result"]["tools"]}
        assert {"run_shell", "write_file", "apply_patch"} <= tool_names

    def test_sensitive_internal_tool_calls_still_go_through_tool_policy_and_approval(self, tmp_path):
        """Verifica que Test sensitive internal tool calls still go through tool policy and approval."""
        approval = MagicMock()
        approval.approve.return_value = False
        executor = ToolExecutor(
            ToolRuntimeConfig(workspace_root=tmp_path),
            approval_handler=approval,
        )
        server = _make_mcp_server(executor)
        inp = io.StringIO(json.dumps({
            "jsonrpc": "2.0",
            "id": 35,
            "method": "tools/call",
            "params": {"name": "run_shell", "arguments": {"command": "pwd"}},
        }) + "\n")
        out = io.StringIO()

        server.serve(stdin=inp, stdout=out)

        approval.approve.assert_called_once()
        response = json.loads(out.getvalue())
        assert response["result"]["isError"] is True
        assert "Execução negada" in response["result"]["content"][0]["text"]

    def test_tools_call_blocks_tools_outside_default_allowlist(self):
        """Verifica que Test tools call blocks tools outside default allowlist."""
        executor = _make_executor(tool_names=["read_file", "run_shell"])
        httpd = _start_http_server(_make_mcp_server(executor))
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "run_shell", "arguments": {"command": "pwd"}},
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            result = json.loads(resp.data)
            assert result["error"]["code"] == -32602
            assert "Tool not allowed: run_shell" in result["error"]["message"]
            executor.execute.assert_not_called()
        finally:
            httpd.shutdown()

    def test_custom_allowlist_can_expose_run_shell(self):
        """Verifica que Test custom allowlist can expose run shell."""
        executor = _make_executor(tool_names=["read_file", "run_shell"])
        httpd = MCP_HTTPServer(
            _make_mcp_server(executor),
            host="127.0.0.1",
            port=0,
            allowed_tools={"run_shell"},
        )
        httpd.start_background()
        _wait_for_server(httpd.host, httpd.port)
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 5, "method": "tools/list",
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            tool_names = {
                tool["name"] for tool in json.loads(resp.data)["result"]["tools"]
            }
            assert tool_names == {"run_shell"}
        finally:
            httpd.shutdown()

    def test_tools_call_no_session_error_result(self):
        """tools/call com erro retorna isError: true (não JSON-RPC error)."""
        result = ToolResult(ok=False, tool_name="read_file", error="Arquivo não encontrado")
        executor = _make_executor(call_result=result)
        httpd = _start_http_server(_make_mcp_server(executor))
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {}},
            }).encode("utf-8")
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            result_data = json.loads(resp.data)
            assert result_data["result"]["isError"] is True
            assert "Arquivo não encontrado" in result_data["result"]["content"][0]["text"]
        finally:
            httpd.shutdown()

    def test_tools_call_via_sse_delivers_result_to_sse_channel(self):
        """tools/call via SSE: resultado chega pelo canal SSE (202 no POST)."""
        import time as _time
        result = ToolResult(ok=True, tool_name="read_file", content="sse result")
        executor = _make_executor(call_result=result)
        mcp = _make_mcp_server(executor)
        httpd = MCP_HTTPServer(mcp, host="127.0.0.1", port=0)
        httpd.start_background()
        _wait_for_server(httpd.host, httpd.port)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((httpd.host, httpd.port))
            s.sendall(b"GET /sse HTTP/1.1\r\nHost: localhost\r\n\r\n")

            # Lê até receber event: endpoint
            chunks = b""
            while b"event: endpoint" not in chunks:
                data = s.recv(4096)
                assert data, "SSE connection closed unexpectedly"
                chunks += data

            body_part = chunks.decode("utf-8", errors="replace")
            http_body = body_part.split("\r\n\r\n", 1)[-1]
            endpoint_path = None
            for line in http_body.split("\n"):
                if "data:" in line and "/message?" in line:
                    from urllib.parse import urlparse
                    raw = line.split("data:", 1)[-1].strip()
                    parsed = urlparse(raw)
                    endpoint_path = parsed.path + "?" + parsed.query
                    break

            assert endpoint_path, "endpoint_path não encontrado"

            # Envia tools/call via POST com sessionId
            call_body = json.dumps({
                "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {}},
            }).encode("utf-8")
            conn = HTTPConnection(httpd.host, httpd.port, timeout=5)
            conn.request("POST", endpoint_path, body=call_body,
                         headers={"Content-Type": "application/json"})
            post_resp = conn.getresponse()
            assert post_resp.status == 202
            post_resp.read()
            conn.close()

            # Aguarda resultado via SSE
            deadline = _time.time() + 5
            while _time.time() < deadline:
                data = s.recv(4096)
                if not data:
                    break
                chunks += data
                if b'"id": 10' in chunks or b'"id":10' in chunks:
                    break

            s.close()
            assert b"event: message" in chunks
            assert b'"id": 10' in chunks or b'"id":10' in chunks
            assert b"sse result" in chunks
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_shutdown_clears_sse_clients(self):
        """Verifica que Test shutdown clears sse clients."""
        mcp = _make_mcp_server()
        httpd = MCP_HTTPServer(mcp, host="127.0.0.1", port=0)
        httpd.start_background()
        _wait_for_server(httpd.host, httpd.port)

        conn = HTTPConnection(httpd.host, httpd.port, timeout=5)
        conn.request("GET", "/sse")
        conn.getresponse()

        httpd.shutdown()

        assert len(httpd._sse_clients) == 0

    def test_shutdown_without_start_does_not_raise(self):
        """Verifica que Test shutdown without start does not raise."""
        mcp = _make_mcp_server()
        httpd = MCP_HTTPServer(mcp)
        httpd.shutdown()

class TestStreamableHTTP:
    def test_mcp_post_initialize_cria_sessao_e_exige_header_de_versao_depois(self):
        """Verifica que Test mcp post initialize cria sessao e exige header de versao depois."""
        httpd = _start_http_server(_make_mcp_server())
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test"}},
            }).encode("utf-8")
            resp = _http_request(httpd.host, httpd.port, "POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
            assert resp.status == 200
            assert resp.header("MCP-Session-Id")
            session_id = resp.header("MCP-Session-Id")
            assert httpd._http_sessions[session_id]["trusted_run_id"].startswith("http:")
            assert httpd._http_sessions[session_id]["trusted_run_id"] != session_id
            assert json.loads(resp.data)["result"]["protocolVersion"] == "2025-11-25"

            bad = _http_request(httpd.host, httpd.port, "POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(), headers={"Content-Type": "application/json", "MCP-Session-Id": resp.header("MCP-Session-Id")})
            assert bad.status == 400

            ok = _http_request(httpd.host, httpd.port, "POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).encode(), headers={"Content-Type": "application/json", "MCP-Session-Id": resp.header("MCP-Session-Id"), "MCP-Protocol-Version": "2025-11-25"})
            assert ok.status == 200
            assert "tools" in json.loads(ok.data)["result"]
        finally:
            httpd.shutdown()

    def test_mcp_http_respeita_bearer_token(self):
        """Verifica que Test mcp http respeita bearer token."""
        mcp = MCPServer(_make_executor(), auth_token="secret")
        httpd = _start_http_server(mcp)
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}).encode()
            denied = _http_request(httpd.host, httpd.port, "POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
            assert denied.status == 401
            allowed = _http_request(httpd.host, httpd.port, "POST", "/mcp", body=body, headers={"Content-Type": "application/json", "Authorization": "Bearer secret"})
            assert allowed.status == 200
        finally:
            httpd.shutdown()

    def test_mcp_http_aplica_token_em_sse_e_message_legados(self):
        """Verifica que Test mcp http aplica token em sse e message legados."""
        mcp = MCPServer(_make_executor(), auth_token="secret")
        httpd = _start_http_server(mcp)
        try:
            denied_sse = _http_request(httpd.host, httpd.port, "GET", "/sse")
            assert denied_sse.status == 401

            body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode()
            denied_message = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body, headers={"Content-Type": "application/json"},
            )
            assert denied_message.status == 401

            allowed_message = _http_request(
                httpd.host, httpd.port, "POST", "/message",
                body=body,
                headers={"Content-Type": "application/json", "X-Quimera-MCP-Token": "secret"},
            )
            assert allowed_message.status == 200
            assert json.loads(allowed_message.data)["result"] == {}
        finally:
            httpd.shutdown()

class TestHTTPTrustedSessions:
    def test_message_without_session_does_not_grow_http_sessions(self):
        """Verifica que Test message without session does not grow http sessions."""
        result = ToolResult(ok=True, tool_name="read_file", content="ok")
        executor = _make_executor(call_result=result)
        httpd = _start_http_server(_make_mcp_server(executor))
        try:
            before = len(httpd._http_sessions)
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
            }).encode("utf-8")
            for _ in range(5):
                resp = _http_request(
                    httpd.host, httpd.port, "POST", "/message",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 200
            assert len(httpd._http_sessions) == before
        finally:
            httpd.shutdown()

    def test_mcp_rejects_unknown_session_id(self):
        """Verifica que Test mcp rejects unknown session id."""
        httpd = _start_http_server(_make_mcp_server())
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/mcp",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "MCP-Session-Id": "attacker-session",
                    "MCP-Protocol-Version": "2025-11-25",
                },
            )
            assert resp.status == 400
            assert b"Unknown MCP-Session-Id" in resp.data
        finally:
            httpd.shutdown()

    def test_message_rejects_unknown_session_id(self):
        """Verifica que Test message rejects unknown session id."""
        httpd = _start_http_server(_make_mcp_server())
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/message?sessionId=attacker-session",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert b"Unknown sessionId" in resp.data
        finally:
            httpd.shutdown()

    def test_initialize_rejects_client_supplied_session_id(self):
        """Verifica que Test initialize rejects client supplied session id."""
        httpd = _start_http_server(_make_mcp_server())
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
            resp = _http_request(
                httpd.host, httpd.port, "POST", "/mcp",
                body=body,
                headers={"Content-Type": "application/json", "MCP-Session-Id": "chosen-by-client"},
            )
            assert resp.status == 400
            assert b"must not be sent" in resp.data
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# OAuth metadata endpoints (OpenAI Secure MCP Tunnel compatibility)
# ---------------------------------------------------------------------------

class TestOAuthMetadata:
    """Testa os endpoints de discovery OAuth exigidos pelo tunnel-client da OpenAI."""

    def test_oauth_protected_resource_returns_200(self):
        """Verifica que Test oauth protected resource returns 200."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
            )
            assert resp.status == 200
        finally:
            httpd.shutdown()

    def test_oauth_protected_resource_content_type_is_json(self):
        """Verifica que Test oauth protected resource content type is json."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
            )
            ct = resp.header("content-type") or ""
            assert "application/json" in ct
        finally:
            httpd.shutdown()

    def test_oauth_protected_resource_body_has_required_fields(self):
        """Verifica que Test oauth protected resource body has required fields."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": f"{httpd.host}:{httpd.port}"},
            )
            data = json.loads(resp.data)
            assert "resource" in data
            # authorization_servers é omitido intencionalmente: o Quimera usa
            # Bearer tokens pré-configurados, sem fluxo OAuth.
            assert "authorization_servers" not in data
            assert "bearer_methods_supported" in data
            assert "header" in data["bearer_methods_supported"]
        finally:
            httpd.shutdown()

    def test_oauth_protected_resource_resource_url_ends_with_mcp(self):
        """Verifica que Test oauth protected resource resource url ends with mcp."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": f"{httpd.host}:{httpd.port}"},
            )
            data = json.loads(resp.data)
            assert data["resource"].endswith("/mcp")
        finally:
            httpd.shutdown()

    def test_oauth_protected_resource_does_not_expose_sensitive_data(self):
        """Verifica que Test oauth protected resource does not expose sensitive data."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
            )
            raw = resp.data.decode("utf-8")
            assert "token" not in raw.lower() or "bearer_methods" in raw
            assert "socket" not in raw
            assert "QUIMERA" not in raw
        finally:
            httpd.shutdown()

    def test_oauth_protected_resource_has_cors_headers(self):
        """Verifica que Test oauth protected resource has cors headers."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
            )
            assert resp.header("access-control-allow-origin") == "*"
        finally:
            httpd.shutdown()

    def test_oauth_protected_resource_requires_no_auth(self):
        """Discovery endpoints são públicos — não exigem Authorization."""
        mcp = _make_mcp_server()
        mcp._auth_token = "supersecret"
        httpd = MCP_HTTPServer(mcp, host="127.0.0.1", port=0)
        httpd.start_background()
        _wait_for_server(httpd.host, httpd.port)
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/mcp",
            )
            assert resp.status == 200
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_returns_200(self):
        """Verifica que Test oauth authorization server returns 200."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
            )
            assert resp.status == 200
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_content_type_is_json(self):
        """Verifica que Test oauth authorization server content type is json."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
            )
            ct = resp.header("content-type") or ""
            assert "application/json" in ct
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_body_has_required_fields(self):
        """Verifica que Test oauth authorization server body has required fields."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
                headers={"Host": f"{httpd.host}:{httpd.port}"},
            )
            data = json.loads(resp.data)
            assert "issuer" in data
            # Endpoints OAuth omitidos: Quimera usa Bearer token pré-configurado.
            # Anunciá-los causaria 404 em clientes que tentam auto-seguir o fluxo.
            assert "authorization_endpoint" not in data
            assert "token_endpoint" not in data
            assert "grant_types_supported" not in data
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_issuer_matches_host(self):
        """Verifica que Test oauth authorization server issuer matches host."""
        httpd = _start_http_server()
        try:
            host_header = f"{httpd.host}:{httpd.port}"
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
                headers={"Host": host_header},
            )
            data = json.loads(resp.data)
            assert data["issuer"] == f"http://{host_header}"
            assert "authorization_endpoint" not in data
            assert "token_endpoint" not in data
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_has_cors_headers(self):
        """Verifica que Test oauth authorization server has cors headers."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
            )
            assert resp.header("access-control-allow-origin") == "*"
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_requires_no_auth(self):
        """Discovery endpoints são públicos — não exigem Authorization."""
        mcp = _make_mcp_server()
        mcp._auth_token = "supersecret"
        httpd = MCP_HTTPServer(mcp, host="127.0.0.1", port=0)
        httpd.start_background()
        _wait_for_server(httpd.host, httpd.port)
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
            )
            assert resp.status == 200
        finally:
            httpd.shutdown()

    def test_oauth_authorization_server_does_not_expose_sensitive_data(self):
        """Verifica que Test oauth authorization server does not expose sensitive data."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-authorization-server",
            )
            raw = resp.data.decode("utf-8")
            assert "socket" not in raw
            assert "QUIMERA" not in raw
            data = json.loads(raw)
            # Apenas issuer é publicado; endpoints OAuth omitidos.
            assert "secret" not in data.get("issuer", "")
            assert "Bearer" not in data.get("issuer", "")
        finally:
            httpd.shutdown()

    def test_unknown_wellknown_returns_404(self):
        """Verifica que Test unknown wellknown returns 404."""
        httpd = _start_http_server()
        try:
            resp = _http_request(
                httpd.host, httpd.port, "GET",
                "/.well-known/oauth-protected-resource/other",
            )
            assert resp.status == 404
        finally:
            httpd.shutdown()
