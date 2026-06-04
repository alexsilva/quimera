"""MCP HTTP+SSE Server: expõe o MCPServer via HTTP com Server-Sent Events.

Endpoints:
  GET  /sse             — estabelece conexão SSE, recebe eventos MCP
  POST /message         — envia mensagem JSON-RPC para o MCPServer
  GET  /health          — healthcheck

Uso:
    executor = ToolExecutor(config, approval_handler)
    mcp = MCPServer(executor)
    httpd = MCP_HTTPServer(mcp)
    httpd.serve_forever()
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
import secrets
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlparse

from quimera.runtime.mcp.server import MCPServer

_logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 1024 * 1024  # 1MB

_QUIMERA_MCP_HTTP_HOST = "QUIMERA_MCP_HTTP_HOST"
_QUIMERA_MCP_HTTP_PORT = "QUIMERA_MCP_HTTP_PORT"
_QUIMERA_MCP_HTTP_CORS_ORIGINS = "QUIMERA_MCP_HTTP_CORS_ORIGINS"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080

HTTP_READ_LOCAL_TOOLS = frozenset({
    "list_files",
    "read_file",
    "grep_search",
    "list_tasks",
    "list_jobs",
    "get_job",
    "todo_list",
})

HTTP_READ_TOOLS = frozenset({
    *HTTP_READ_LOCAL_TOOLS,
    "web_search",
    "web_fetch",
})

HTTP_AGENT_TOOLS = frozenset({
    *HTTP_READ_TOOLS,
    "call_agent",
})

HTTP_TOOL_PROFILES: dict[str, frozenset[str] | None] = {
    "read-local": HTTP_READ_LOCAL_TOOLS,
    "read": HTTP_READ_TOOLS,
    "agent": HTTP_AGENT_TOOLS,
    "all": None,
}

DEFAULT_HTTP_READ_ONLY_TOOLS = HTTP_READ_TOOLS
DEFAULT_HTTP_TOOL_PROFILE = "read"


class _SSEQueueOutput:
    """Writable stream-like object that puts JSON-RPC objects into an SSE queue."""

    def __init__(self, sse_queue: queue.Queue) -> None:
        self._queue = sse_queue

    def write(self, data: str) -> int:
        text = data.rstrip("\n")
        if not text:
            return 0
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return 0
        self._queue.put_nowait(obj)
        return len(data)

    def flush(self) -> None:
        pass


class _MCPHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for MCP HTTP+SSE transport.

    Expects ``self.server.mcp_http_server`` to point to the ``MCP_HTTPServer``.
    """

    def log_message(self, fmt: str, *args: Any) -> None:
        _logger.debug("MCP HTTP: %s", fmt % args)

    # ------------------------------------------------------------------
    # CORS helpers
    # ------------------------------------------------------------------

    def _send_cors(self) -> None:
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        cors_origin = mcp_server._cors_origin_for(self.headers.get("Origin"))
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            if cors_origin != "*":
                self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, MCP-Protocol-Version, MCP-Session-Id, X-Quimera-MCP-Token"
        )
        self.send_header(
            "Access-Control-Expose-Headers",
            "MCP-Session-Id, MCP-Protocol-Version",
        )

    # ------------------------------------------------------------------
    # HTTP method dispatch
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._handle_health()
        if parsed.path == "/sse":
            return self._handle_sse()
        if parsed.path == "/mcp":
            return self._handle_mcp_stream()
        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            return self._handle_mcp_post()
        if parsed.path.startswith("/message"):
            return self._handle_message()
        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
            self.send_response(404); self._send_cors(); self.end_headers(); return
        if not self._is_authorized():
            self._send_error_response(401, -32001, "Unauthorized")
            return
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        session_id = self.headers.get("MCP-Session-Id")
        if session_id:
            with mcp_server._sse_lock:
                mcp_server._http_sessions.pop(session_id, None)
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    # ------------------------------------------------------------------
    # GET /health
    # ------------------------------------------------------------------

    def _handle_health(self) -> None:
        self.send_response(200)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps({
            "status": "ok",
            "server": MCPServer.SERVER_NAME,
        })
        self.wfile.write(body.encode("utf-8"))

    # ------------------------------------------------------------------
    # GET /sse
    # ------------------------------------------------------------------

    def _handle_sse(self) -> None:
        if not self._is_authorized():
            self._send_error_response(401, -32001, "Unauthorized")
            return
        session_id = str(uuid.uuid4())
        sse_queue: queue.Queue = queue.Queue()

        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        with mcp_server._sse_lock:
            mcp_server._sse_clients[session_id] = sse_queue

        try:
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("MCP-Protocol-Version", MCPServer.PROTOCOL_VERSION)
            self.end_headers()

            endpoint_url = f"/message?sessionId={session_id}"
            self.wfile.write(
                f"event: endpoint\ndata: {endpoint_url}\n\n".encode("utf-8")
            )
            self.wfile.flush()

            while True:
                try:
                    event_data = sse_queue.get(timeout=30)
                except queue.Empty:
                    self.wfile.write(": keepalive\n\n".encode("utf-8"))
                    self.wfile.flush()
                    continue

                if event_data is None:
                    break

                payload = json.dumps(event_data, ensure_ascii=False)
                self.wfile.write(
                    f"event: message\ndata: {payload}\n\n".encode("utf-8")
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("SSE client %s disconnected", session_id)
        finally:
            with mcp_server._sse_lock:
                mcp_server._sse_clients.pop(session_id, None)

    # ------------------------------------------------------------------
    # POST /message
    # ------------------------------------------------------------------

    def _is_authorized(self) -> bool:
        token = (getattr(self.server.mcp_http_server._mcp, "_auth_token", None) or "").strip()
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        x_token = self.headers.get("X-Quimera-MCP-Token", "")
        return auth == f"Bearer {token}" or x_token == token

    def _handle_mcp_stream(self) -> None:
        if not self._is_authorized():
            self._send_error_response(401, -32001, "Unauthorized")
            return
        # Streamable HTTP GET: keep a server-to-client SSE stream for a session.
        return self._handle_sse()

    def _handle_mcp_post(self) -> None:
        if not self._is_authorized():
            self._send_error_response(401, -32001, "Unauthorized")
            return
        proto = self.headers.get("MCP-Protocol-Version")
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > _MAX_BODY_SIZE:
            self._send_error_response(413, -32600, "Request body too large")
            return
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        try:
            msg = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            self._send_error_response(400, -32700, f"Parse error: {exc}")
            return
        is_initialize = isinstance(msg, dict) and msg.get("method") == "initialize"
        if proto and proto not in MCPServer.SUPPORTED_PROTOCOL_VERSIONS:
            self._send_error_response(400, -32602, f"Unsupported MCP-Protocol-Version: {proto}")
            return
        if not is_initialize and proto is None:
            # Latest spec requires this header on subsequent HTTP requests.
            self._send_error_response(400, -32602, "Missing MCP-Protocol-Version header")
            return
        if not isinstance(msg, (dict, list)):
            self._send_error_response(400, -32600, "Invalid Request: body must be a JSON object or array")
            return
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        session_id = self.headers.get("MCP-Session-Id")
        if is_initialize and not session_id:
            session_id = secrets.token_urlsafe(24)
        out = StringIO()
        if session_id:
            state = mcp_server._http_sessions.setdefault(session_id, {"initialize_seen": False, "initialized": False, "strict_lifecycle": True})
            setattr(out, "_mcp_state", state)
        try:
            mcp_server._mcp._process_message(msg, out=out)
            mcp_server._mcp._drain_all_pending(out)
        except Exception as exc:
            _logger.exception("MCP HTTP: error handling /mcp")
            error_resp = mcp_server._mcp._err(msg.get("id") if isinstance(msg, dict) else None, -32603, f"Internal error: {exc}")
            raw = json.dumps(error_resp) + "\n"
        else:
            raw = out.getvalue()
        body_bytes = raw.encode("utf-8") if raw else b""
        self.send_response(200 if raw else 202)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header("MCP-Session-Id", session_id)
        self.send_header("MCP-Protocol-Version", MCPServer.PROTOCOL_VERSION)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def _handle_message(self) -> None:
        if not self._is_authorized():
            self._send_error_response(401, -32001, "Unauthorized")
            return
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > _MAX_BODY_SIZE:
            self._send_error_response(413, -32600, "Request body too large")
            return
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)
        session_ids = query_params.get("sessionId") or query_params.get(
            "session_id"
        )
        session_id = session_ids[0] if session_ids else None

        try:
            msg = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            self._send_error_response(400, -32700, f"Parse error: {exc}")
            return

        if not isinstance(msg, (dict, list)):
            self._send_error_response(
                400, -32600, "Invalid Request: body must be a JSON object or array"
            )
            return

        mcp_server: MCP_HTTPServer = self.server.mcp_http_server

        out: StringIO | _SSEQueueOutput
        sse_queue = None
        if session_id:
            with mcp_server._sse_lock:
                sse_queue = mcp_server._sse_clients.get(session_id)
        if sse_queue is not None:
            out = _SSEQueueOutput(sse_queue)
        else:
            out = StringIO()

        try:
            mcp_server._mcp._process_message(msg, out=out)
            # Para requisições sem canal SSE, aguarda conclusão de tools/call
            # assíncronas antes de ler o StringIO (evita resposta vazia).
            if isinstance(out, StringIO):
                mcp_server._mcp._drain_all_pending(out)
        except Exception as exc:
            _logger.exception("MCP HTTP: error handling message")
            error_resp = mcp_server._mcp._err(
                msg.get("id") if isinstance(msg, dict) else None,
                -32603, f"Internal error: {exc}",
            )
            body_bytes = json.dumps(error_resp).encode("utf-8")
            self.send_response(500)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        if isinstance(out, StringIO):
            raw = out.getvalue()
            if raw:
                body_bytes = raw.encode("utf-8")
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

        self.send_response(202)
        self._send_cors()
        self.end_headers()

    def _send_error_response(
        self, status: int, code: int, message: str
    ) -> None:
        error_resp = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": message},
        }
        body_bytes = json.dumps(error_resp).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


class MCP_HTTPServer:
    """Wrapper HTTP+SSE para MCPServer.

    Expõe o servidor MCP via HTTP, usando SSE para notificações do servidor
    e POST /message para envio de mensagens JSON-RPC.

    Attributes:
        host: Host do servidor HTTP.
        port: Porta do servidor HTTP.
    """

    def __init__(
        self,
        mcp_server: MCPServer,
        host: str = "",
        port: int = 0,
        allowed_tools: Iterable[str] | None = DEFAULT_HTTP_READ_ONLY_TOOLS,
        cors_origins: str | Iterable[str] | None = None,
    ) -> None:
        self._mcp = mcp_server
        self._mcp.set_allowed_tools(allowed_tools)
        self._cors_origins = self._normalize_cors_origins(cors_origins)
        self._host = host or os.environ.get(
            _QUIMERA_MCP_HTTP_HOST, _DEFAULT_HOST
        )
        self._port = port or int(
            os.environ.get(_QUIMERA_MCP_HTTP_PORT, str(_DEFAULT_PORT))
        )
        self._sse_clients: dict[str, queue.Queue] = {}
        self._http_sessions: dict[str, dict] = {}
        self._sse_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None


    @staticmethod
    def _normalize_cors_origins(
        cors_origins: str | Iterable[str] | None,
    ) -> frozenset[str]:
        if cors_origins is None:
            raw = os.environ.get(_QUIMERA_MCP_HTTP_CORS_ORIGINS, "*")
            items: Iterable[str] = raw.split(",")
        elif isinstance(cors_origins, str):
            items = cors_origins.split(",")
        else:
            items = cors_origins
        normalized = frozenset(
            str(origin).strip() for origin in items if str(origin).strip()
        )
        return normalized or frozenset({"*"})

    @property
    def cors_origins(self) -> frozenset[str]:
        """Origens CORS permitidas; ``{"*"}`` mantém o padrão de desenvolvimento."""
        return self._cors_origins

    def _cors_origin_for(self, request_origin: str | None) -> str | None:
        if "*" in self._cors_origins:
            return "*"
        origin = (request_origin or "").strip()
        if origin and origin in self._cors_origins:
            return origin
        return None

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        """Allowlist efetiva de tools publicada por este transporte HTTP."""
        return self._mcp.allowed_tools

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def httpd(self) -> ThreadingHTTPServer | None:
        return self._httpd

    def serve_forever(self) -> None:
        """Inicia o servidor HTTP e bloqueia até ser interrompido."""
        server = ThreadingHTTPServer(
            (self._host, self._port), _MCPHTTPRequestHandler
        )
        server.mcp_http_server = self
        self._httpd = server
        self._mcp._start_background_flush()
        _logger.info(
            "MCP HTTP+SSE server listening on http://%s:%d",
            self._host,
            self._port,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._mcp._stop_background_flush()
            server.server_close()
            _logger.info("MCP HTTP+SSE server stopped")

    def start_background(self) -> None:
        """Inicia o servidor HTTP em uma thread daemon e retorna imediatamente."""
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()

    def shutdown(self) -> None:
        """Para o servidor HTTP e sinaliza todas as conexões SSE."""
        self._mcp._stop_background_flush()
        self._mcp.shutdown()
        if self._httpd:
            self._httpd.shutdown()
        with self._sse_lock:
            for q in self._sse_clients.values():
                q.put_nowait(None)
            self._sse_clients.clear()


def create_server(
    mcp_server: MCPServer,
    host: str = "",
    port: int = 0,
    allowed_tools: Iterable[str] | None = DEFAULT_HTTP_READ_ONLY_TOOLS,
    cors_origins: str | Iterable[str] | None = None,
) -> MCP_HTTPServer:
    """Cria uma instância de MCP_HTTPServer sem iniciá-la.

    Args:
        mcp_server: Instância de MCPServer a ser exposta via HTTP.
        host: Host para bind (padrão: QUIMERA_MCP_HTTP_HOST ou 127.0.0.1).
        port: Porta para bind (padrão: QUIMERA_MCP_HTTP_PORT ou 8080).
        allowed_tools: Allowlist de tools expostas via HTTP. Por padrão,
            publica apenas tools de leitura; use ``None`` para expor todas.
        cors_origins: Origens CORS permitidas. Quando omitido, lê
            ``QUIMERA_MCP_HTTP_CORS_ORIGINS`` e usa ``*`` como padrão de desenvolvimento.

    Returns:
        MCP_HTTPServer configurado mas não iniciado.
    """
    return MCP_HTTPServer(
        mcp_server,
        host=host,
        port=port,
        allowed_tools=allowed_tools,
        cors_origins=cors_origins,
    )
