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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlparse

from quimera.runtime.mcp.server import MCPServer

_logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 1024 * 1024  # 1MB

_QUIMERA_MCP_HTTP_HOST = "QUIMERA_MCP_HTTP_HOST"
_QUIMERA_MCP_HTTP_PORT = "QUIMERA_MCP_HTTP_PORT"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080


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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization"
        )

    # ------------------------------------------------------------------
    # HTTP method dispatch
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._handle_health()
        if self.path == "/sse":
            return self._handle_sse()
        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def do_POST(self) -> None:
        if self.path.startswith("/message"):
            return self._handle_message()
        self.send_response(404)
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
            self.end_headers()

            host = self.headers.get("Host", "localhost")
            endpoint_url = f"http://{host}/message?sessionId={session_id}"
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

    def _handle_message(self) -> None:
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
        if session_id and session_id in mcp_server._sse_clients:
            sse_queue = mcp_server._sse_clients[session_id]
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
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        error_resp = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": message},
        }
        self.wfile.write(json.dumps(error_resp).encode("utf-8"))


class MCP_HTTPServer:
    """Wrapper HTTP+SSE para MCPServer.

    Expõe o servidor MCP via HTTP, usando SSE para notificações do servidor
    e POST /message para envio de mensagens JSON-RPC.

    Attributes:
        host: Host do servidor HTTP.
        port: Porta do servidor HTTP.
    """

    def __init__(
        self, mcp_server: MCPServer, host: str = "", port: int = 0
    ) -> None:
        self._mcp = mcp_server
        self._host = host or os.environ.get(
            _QUIMERA_MCP_HTTP_HOST, _DEFAULT_HOST
        )
        self._port = port or int(
            os.environ.get(_QUIMERA_MCP_HTTP_PORT, str(_DEFAULT_PORT))
        )
        self._sse_clients: dict[str, queue.Queue] = {}
        self._sse_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None

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
            server.server_close()
            _logger.info("MCP HTTP+SSE server stopped")

    def start_background(self) -> None:
        """Inicia o servidor HTTP em uma thread daemon e retorna imediatamente."""
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()

    def shutdown(self) -> None:
        """Para o servidor HTTP e sinaliza todas as conexões SSE."""
        self._mcp._stop_background_flush()
        if self._httpd:
            self._httpd.shutdown()
        with self._sse_lock:
            for q in self._sse_clients.values():
                q.put_nowait(None)
            self._sse_clients.clear()


def create_server(
    mcp_server: MCPServer, host: str = "", port: int = 0
) -> MCP_HTTPServer:
    """Cria uma instância de MCP_HTTPServer sem iniciá-la.

    Args:
        mcp_server: Instância de MCPServer a ser exposta via HTTP.
        host: Host para bind (padrão: QUIMERA_MCP_HTTP_HOST ou 127.0.0.1).
        port: Porta para bind (padrão: QUIMERA_MCP_HTTP_PORT ou 8080).

    Returns:
        MCP_HTTPServer configurado mas não iniciado.
    """
    return MCP_HTTPServer(mcp_server, host=host, port=port)
