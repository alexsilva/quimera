"""MCP HTTP+SSE Server: expõe o MCPServer via HTTP com Server-Sent Events.

Endpoints MCP:
  GET  /sse             — estabelece conexão SSE, recebe eventos MCP
  POST /message         — envia mensagem JSON-RPC para o MCPServer
  GET  /mcp             — stream SSE do transporte Streamable HTTP
  POST /mcp             — mensagem JSON-RPC do transporte Streamable HTTP
  GET  /health          — healthcheck

Endpoints OAuth:
  GET  /.well-known/oauth-protected-resource[/mcp]  — RFC 9728
  GET  /.well-known/oauth-authorization-server      — RFC 8414
  GET  /oauth/authorize  — tela de consentimento
  POST /oauth/authorize  — decisão do usuário
  POST /oauth/token      — authorization_code / refresh_token / client_credentials
  POST /oauth/register   — Dynamic Client Registration (RFC 7591)
  POST /oauth/revoke     — RFC 7009
  POST /oauth/introspect — RFC 7662

Autenticação: Bearer OAuth. O transporte HTTP não aceita o token interno usado
pelo transporte socket.

Uso:
    executor = ToolExecutor(config, approval_handler)
    mcp = MCPServer(executor)
    httpd = MCP_HTTPServer(mcp, oauth=OAuthConfig(enabled=True))
    httpd.serve_forever()
"""
from __future__ import annotations

import base64
import binascii
import errno
import json
import logging
import os
import queue
import sys
import threading
import uuid
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlparse

from quimera.runtime.mcp.oauth import (
    AuthContext,
    OAuthConfig,
    OAuthError,
    OAuthProvider,
    OAuthRedirectError,
)
from quimera.runtime.mcp.server import MCPServer

_logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 1024 * 1024  # 1MB


@dataclass(frozen=True)
class ConnectedMCPClient:
    """Client OAuth conhecido pelo transporte MCP HTTP."""

    session_id: str
    client_id: str
    client_name: str
    scope: str
    profile: str
    initialized: bool
    connected: bool = True
    authorized: bool = True


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that treats client disconnects as normal."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            _logger.debug("MCP HTTP: client disconnected from %s", client_address)
            return
        if isinstance(exc, OSError) and exc.errno in {
            errno.ECONNABORTED,
            errno.ECONNRESET,
            errno.EPIPE,
        }:
            _logger.debug("MCP HTTP: client disconnected from %s", client_address)
            return
        super().handle_error(request, client_address)

_QUIMERA_MCP_HTTP_HOST = "QUIMERA_MCP_HTTP_HOST"
_QUIMERA_MCP_HTTP_PORT = "QUIMERA_MCP_HTTP_PORT"
_QUIMERA_MCP_HTTP_CORS_ORIGINS = "QUIMERA_MCP_HTTP_CORS_ORIGINS"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080


@dataclass(frozen=True)
class ActiveMCPHTTPClient:
    """Snapshot de um client MCP autenticado com sessão HTTP ativa."""

    session_id: str
    oauth_client_id: str
    oauth_client_name: str
    mcp_client_name: str
    mcp_client_version: str
    scope: str
    protocol_version: str

HTTP_READ_LOCAL_TOOLS = frozenset({
    "list_files",
    "read_file",
    "grep_search",
    "inspect_symbols",
    "list_tasks",
    "list_jobs",
    "get_job",
    "memory_retrieve",
    "memory_list_namespaces",
    "todo_list",
    # Git read-only tools
    "git_status",
    "git_log",
    "git_diff",
    "git_branch",
    "git_fetch",
})

HTTP_READ_TOOLS = frozenset({
    *HTTP_READ_LOCAL_TOOLS,
    "web_search",
    "web_fetch",
})

HTTP_AGENT_TOOLS = frozenset({
    *HTTP_READ_TOOLS,
    "replace_text",
    "memory_save",
    "memory_delete",
    "http_request",
    "delegate",
    "list_agents",
    "tasks",
    # Git mutation tools (require approval)
    "git_add",
    "git_commit",
    "git_checkout",
    "git_push",
})

HTTP_TOOL_PROFILES: dict[str, frozenset[str] | None] = {
    "read-local": HTTP_READ_LOCAL_TOOLS,
    "read": HTTP_READ_TOOLS,
    "agent": HTTP_AGENT_TOOLS,
    "all": None,
}

DEFAULT_HTTP_READ_ONLY_TOOLS = HTTP_READ_TOOLS
DEFAULT_HTTP_TOOL_PROFILE = "read"

_OAUTH_PROTECTED_RESOURCE_PATHS = frozenset({
    OAuthProvider.METADATA_PR_PATH,
    f"{OAuthProvider.METADATA_PR_PATH}{OAuthProvider.RESOURCE_PATH}",
})

_OAUTH_AUTHORIZATION_SERVER_PATHS = frozenset({
    OAuthProvider.METADATA_AS_PATH,
    f"{OAuthProvider.METADATA_AS_PATH}{OAuthProvider.RESOURCE_PATH}",
})


def _is_loopback_host(host: str) -> bool:
    """Indica se *host* (com porta opcional) aponta para a máquina local."""
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    hostname = hostname.strip("[]").lower()
    return hostname in ("127.0.0.1", "localhost", "::1", "")


def _set_mcp_state(out: Any, state: dict) -> None:
    """Anexa o estado de sessão MCP ao stream de saída quando suportado."""
    try:
        setattr(out, "_mcp_state", state)
    except Exception:
        pass


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
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, MCP-Protocol-Version, MCP-Session-Id, "
            "X-Quimera-Agent, X-Quimera-Run, "
            "X-Quimera-Trace, X-Quimera-Parent-Run",
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
        if parsed.path in _OAUTH_PROTECTED_RESOURCE_PATHS:
            return self._handle_oauth_protected_resource()
        if parsed.path in _OAUTH_AUTHORIZATION_SERVER_PATHS:
            return self._handle_oauth_authorization_server()
        if parsed.path == OAuthProvider.AUTHORIZE_PATH:
            return self._handle_oauth_authorize_get(parsed)
        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            return self._handle_mcp_post()
        if parsed.path.startswith("/message"):
            return self._handle_message()
        if parsed.path == OAuthProvider.AUTHORIZE_PATH:
            return self._handle_oauth_authorize_post()
        if parsed.path == OAuthProvider.TOKEN_PATH:
            return self._handle_oauth_token()
        if parsed.path == OAuthProvider.REGISTER_PATH:
            return self._handle_oauth_register()
        if parsed.path == OAuthProvider.REVOKE_PATH:
            return self._handle_oauth_revoke()
        if parsed.path == OAuthProvider.INTROSPECT_PATH:
            return self._handle_oauth_introspect()
        self.send_response(404)
        self._send_cors()
        self.end_headers()

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
            self.send_response(404)
            self._send_cors()
            self.end_headers()
            return
        if self._require_auth() is None:
            return
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        session_id = self.headers.get("MCP-Session-Id")
        if session_id:
            with mcp_server._sse_lock:
                mcp_server._http_sessions.pop(session_id, None)
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def _apply_quimera_run_headers(self, state: dict) -> None:
        """Propaga identidade visual confiável do transporte HTTP para tools/call."""
        agent_name = str(self.headers.get("X-Quimera-Agent") or "").strip()
        run_id = str(self.headers.get("X-Quimera-Run") or "").strip()
        trace_id = str(self.headers.get("X-Quimera-Trace") or "").strip()
        parent_run_id = str(self.headers.get("X-Quimera-Parent-Run") or "").strip()
        state["agent_name"] = agent_name or state.get("agent_name") or "mcp-http"
        if run_id:
            state["trusted_run_id"] = run_id
        if trace_id:
            state["trace_id"] = trace_id
        if parent_run_id:
            state["parent_run_id"] = parent_run_id

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
    # GET /.well-known/oauth-protected-resource/mcp  (RFC 9728)
    # GET /.well-known/oauth-authorization-server    (RFC 8414)
    # ------------------------------------------------------------------

    def _base_url_from_request(self) -> str:
        """Constrói a URL base pública da requisição.

        Respeita ``X-Forwarded-Proto``/``X-Forwarded-Host`` para que o fluxo
        OAuth funcione atrás de proxies e túneis (ngrok, cloudflared), onde o
        issuer precisa ser a URL HTTPS externa e não o bind local.
        """
        forwarded_host = str(self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        host = forwarded_host or str(self.headers.get("Host") or "").strip()
        scheme = str(self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        if not host:
            mcp_server: MCP_HTTPServer = self.server.mcp_http_server
            return f"{scheme or 'http'}://{mcp_server.host}:{mcp_server.port}"
        if not scheme:
            scheme = "http" if _is_loopback_host(host) else "https"
        return f"{scheme}://{host}"

    def _handle_oauth_protected_resource(self) -> None:
        """RFC 9728 — OAuth 2.0 Protected Resource Metadata.

        Publica ``authorization_servers`` quando o provider está habilitado,
        permitindo que o client MCP descubra o AS e execute o fluxo completo.
        """
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        metadata = mcp_server.oauth.protected_resource_metadata(self._base_url_from_request())
        self._send_json(200, metadata)

    def _handle_oauth_authorization_server(self) -> None:
        """RFC 8414 — OAuth 2.0 Authorization Server Metadata.

        Quando OAuth está desabilitado, publica apenas ``issuer`` e omite
        ``authorization_endpoint``/``token_endpoint`` para que clientes não
        tentem iniciar fluxos inexistentes (comportamento legado preservado).
        """
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        base = self._base_url_from_request()
        if not mcp_server.oauth.enabled:
            self._send_json(200, {"issuer": mcp_server.oauth.issuer_for(base), "scopes_supported": []})
            return
        self._send_json(200, mcp_server.oauth.authorization_server_metadata(base))

    # ------------------------------------------------------------------
    # Endpoints OAuth
    # ------------------------------------------------------------------

    @property
    def _oauth(self) -> OAuthProvider:
        """Provider OAuth do servidor HTTP associado."""
        return self.server.mcp_http_server.oauth

    def _read_body(self, limit: int = _MAX_BODY_SIZE) -> bytes | None:
        """Lê o corpo da requisição, respondendo 413 se exceder *limit*."""
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            content_length = 0
        if content_length > limit:
            self._send_error_response(413, -32600, "Request body too large")
            return None
        return self.rfile.read(content_length) if content_length else b""

    def _read_form(self) -> dict[str, list[str]] | None:
        """Lê e parseia um corpo ``application/x-www-form-urlencoded``."""
        raw = self._read_body()
        if raw is None:
            return None
        return parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)

    def _basic_auth_credentials(self) -> tuple[str, str] | None:
        """Extrai credenciais de ``Authorization: Basic`` do endpoint de token."""
        header = str(self.headers.get("Authorization") or "").strip()
        if not header.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        client_id, _, client_secret = decoded.partition(":")
        return client_id, client_secret

    def _send_json(self, status: int, payload: dict, extra_headers: dict | None = None) -> None:
        """Envia uma resposta JSON com CORS e ``Content-Length``."""
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("MCP HTTP: client disconnected during JSON response")

    def _send_html(self, status: int, markup: str) -> None:
        """Envia uma página HTML (tela de consentimento OAuth)."""
        body_bytes = markup.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            # A tela de consentimento nunca deve ser embutida por terceiros
            # (clickjacking) nem vazar a URL de autorização via Referer.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("MCP HTTP: client disconnected during HTML response")

    def _send_redirect(self, location: str) -> None:
        """Envia um redirect 302 para *location*."""
        try:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("MCP HTTP: client disconnected during redirect")

    def _send_oauth_error(self, exc: OAuthError) -> None:
        """Serializa um ``OAuthError`` como resposta JSON de erro OAuth."""
        headers = {}
        if exc.status == 401:
            headers["WWW-Authenticate"] = 'Basic realm="quimera-mcp"'
        self._send_json(exc.status, exc.to_dict(), extra_headers=headers)

    def _handle_oauth_authorize_get(self, parsed: Any) -> None:
        """``GET /oauth/authorize`` — valida o pedido e exibe o consentimento."""
        if not self._oauth.enabled:
            self._send_json(404, {"error": "invalid_request", "error_description": "OAuth desabilitado"})
            return
        params = parse_qs(parsed.query, keep_blank_values=True)
        try:
            request = self._oauth.begin_authorization(params)
        except OAuthRedirectError as exc:
            self._send_redirect(
                self._oauth.error_redirect(exc.redirect_uri, exc.state, exc.error, exc.description)
            )
            return
        except OAuthError as exc:
            self._send_oauth_error(exc)
            return
        if self._oauth.config.auto_approve and not self._oauth.config.passcode:
            self._send_redirect(self._oauth.approve_authorization(request))
            return
        self._send_html(200, self._oauth.render_consent_page(request))

    def _handle_oauth_authorize_post(self) -> None:
        """``POST /oauth/authorize`` — aplica a decisão humana do consentimento."""
        if not self._oauth.enabled:
            self._send_json(404, {"error": "invalid_request", "error_description": "OAuth desabilitado"})
            return
        form = self._read_form()
        if form is None:
            return
        request_id = (form.get("request_id") or [""])[0].strip()
        try:
            request = self._oauth.pending_request(request_id)
        except OAuthError as exc:
            self._send_oauth_error(exc)
            return
        decision = (form.get("decision") or [""])[0].strip()
        if decision != "allow":
            self._send_redirect(self._oauth.deny_authorization(request))
            return
        passcode = (form.get("passcode") or [""])[0]
        if not self._oauth.check_passcode(passcode):
            _logger.warning(
                "MCP OAuth: passcode incorreto no consentimento client=%s", request.client_id
            )
            self._send_html(
                401,
                self._oauth.render_consent_page(request, error="Código de acesso incorreto."),
            )
            return
        try:
            self._send_redirect(self._oauth.approve_authorization(request))
        except OAuthError as exc:
            self._send_oauth_error(exc)

    def _handle_oauth_token(self) -> None:
        """``POST /oauth/token`` — emite tokens para os grants suportados."""
        form = self._read_form()
        if form is None:
            return
        try:
            payload = self._oauth.issue_token(form, basic_auth=self._basic_auth_credentials())
        except OAuthError as exc:
            self._send_oauth_error(exc)
            return
        self._send_json(200, payload)

    def _handle_oauth_register(self) -> None:
        """``POST /oauth/register`` — Dynamic Client Registration (RFC 7591)."""
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": "invalid_client_metadata", "error_description": str(exc)})
            return
        if not isinstance(payload, dict):
            self._send_json(
                400,
                {"error": "invalid_client_metadata", "error_description": "corpo deve ser objeto JSON"},
            )
            return
        try:
            client = self._oauth.register_client(payload)
        except OAuthError as exc:
            self._send_json(exc.status, exc.to_dict())
            return
        self._send_json(201, client.registration_response())

    def _handle_oauth_revoke(self) -> None:
        """``POST /oauth/revoke`` — revogação de token (RFC 7009)."""
        form = self._read_form()
        if form is None:
            return
        try:
            self._oauth.revoke(form, basic_auth=self._basic_auth_credentials())
        except OAuthError as exc:
            self._send_oauth_error(exc)
            return
        self._send_json(200, {})

    def _handle_oauth_introspect(self) -> None:
        """``POST /oauth/introspect`` — introspecção de token (RFC 7662)."""
        form = self._read_form()
        if form is None:
            return
        try:
            payload = self._oauth.introspect(form, basic_auth=self._basic_auth_credentials())
        except OAuthError as exc:
            self._send_oauth_error(exc)
            return
        self._send_json(200, payload)

    # ------------------------------------------------------------------
    # GET /sse
    # ------------------------------------------------------------------

    def _handle_sse(self, auth: AuthContext | None = None) -> None:
        if auth is None:
            auth = self._require_auth()
            if auth is None:
                return
        session_id = str(uuid.uuid4())
        sse_queue: queue.Queue = queue.Queue()

        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        with mcp_server._sse_lock:
            mcp_server._sse_clients[session_id] = sse_queue
        mcp_server._http_sessions[session_id] = {
            "initialize_seen": False,
            "initialized": False,
            "strict_lifecycle": False,
            "session_id": session_id,
            "trusted_run_id": f"http:{uuid.uuid4()}",
            "http_profile": mcp_server._http_profile,
            "http_delegate_auto_approve": mcp_server._http_profile in ("agent", "all"),
        }
        self._apply_quimera_run_headers(mcp_server._http_sessions[session_id])
        self._apply_auth_context(mcp_server._http_sessions[session_id], auth)

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
            mcp_server._http_sessions.pop(session_id, None)

    # ------------------------------------------------------------------
    # POST /message
    # ------------------------------------------------------------------

    def _bearer_token(self) -> str:
        """Extrai o Bearer token do header ``Authorization``, se presente."""
        header = str(self.headers.get("Authorization") or "").strip()
        if header[:7].lower() == "bearer ":
            return header[7:].strip()
        return ""

    def _authenticate(self) -> AuthContext:
        """Autentica a requisição exclusivamente por Bearer OAuth."""
        oauth = self._oauth
        if not oauth.enabled:
            return AuthContext(authenticated=True, mode="anonymous")
        bearer = self._bearer_token()
        if bearer:
            return oauth.authenticate_bearer(bearer)
        return AuthContext(
            authenticated=False,
            mode="oauth",
            error="invalid_request",
            error_description="Bearer token OAuth ausente",
        )

    def _require_auth(self) -> AuthContext | None:
        """Aplica a autenticação, respondendo 401 quando ela falha.

        Returns:
            O ``AuthContext`` autenticado, ou ``None`` se a resposta 401 já foi
            enviada (o chamador deve retornar imediatamente).
        """
        auth = self._authenticate()
        if auth.authenticated:
            return auth
        self._send_unauthorized(auth)
        return None

    def _send_unauthorized(self, auth: AuthContext) -> None:
        """Responde 401 com ``WWW-Authenticate`` apontando ao metadata RFC 9728."""
        challenge = self._oauth.www_authenticate(
            self._base_url_from_request(),
            error=auth.error,
            description=auth.error_description,
        )
        error_resp = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32001, "message": auth.error_description or "Unauthorized"},
        }
        self._send_json(401, error_resp, extra_headers={"WWW-Authenticate": challenge})

    def _apply_auth_context(self, state: dict, auth: AuthContext) -> None:
        """Registra a identidade autenticada e o escopo efetivo na sessão MCP."""
        state["auth_mode"] = auth.mode
        if auth.client_id:
            state["oauth_client_id"] = auth.client_id
        if auth.scope:
            state["oauth_scope"] = auth.scope
        mcp_server: MCP_HTTPServer = self.server.mcp_http_server
        if not auth.tool_profile:
            return
        state["http_profile"] = auth.tool_profile
        state["http_delegate_auto_approve"] = auth.tool_profile in ("agent", "all")
        disabled = mcp_server.disabled_tools_for_profile(auth.tool_profile)
        if disabled:
            existing = state.get("quimera_disabled_tools") or ()
            if isinstance(existing, str):
                existing = tuple(part.strip() for part in existing.split(",") if part.strip())
            state["quimera_disabled_tools"] = tuple(sorted(set(existing) | set(disabled)))

    def _handle_mcp_stream(self) -> None:
        auth = self._require_auth()
        if auth is None:
            return
        # Streamable HTTP GET: keep a server-to-client SSE stream for a session.
        return self._handle_sse(auth)

    def _handle_mcp_post(self) -> None:
        auth = self._require_auth()
        if auth is None:
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
        if is_initialize:
            if session_id:
                self._send_error_response(400, -32602, "MCP-Session-Id must not be sent during initialize")
                return
            session_id = secrets.token_urlsafe(24)
            state = {
                "initialize_seen": False,
                "initialized": False,
                "strict_lifecycle": True,
                "session_id": session_id,
                "trusted_run_id": f"http:{uuid.uuid4()}",
                "http_profile": mcp_server._http_profile,
                "http_delegate_auto_approve": mcp_server._http_profile in ("agent", "all"),
            }
            mcp_server._http_sessions[session_id] = state
        elif session_id:
            state = mcp_server._http_sessions.get(session_id)
            if state is None:
                self._send_error_response(404, -32602, "Unknown MCP-Session-Id")
                return
            state["session_id"] = session_id
            state["http_profile"] = mcp_server._http_profile
            state["http_delegate_auto_approve"] = mcp_server._http_profile in ("agent", "all")
        else:
            state = {
                "initialize_seen": False,
                "initialized": False,
                "strict_lifecycle": True,
                "trusted_run_id": f"http:{uuid.uuid4()}",
                "http_profile": mcp_server._http_profile,
                "http_delegate_auto_approve": mcp_server._http_profile in ("agent", "all"),
            }
        self._apply_quimera_run_headers(state)
        self._apply_auth_context(state, auth)
        out = StringIO()
        _set_mcp_state(out, state)
        try:
            mcp_server._mcp._process_message(msg, out=out, transport="http_mcp")
            mcp_server._mcp._drain_all_pending(out)
        except Exception as exc:
            _logger.exception("MCP HTTP: error handling /mcp")
            error_resp = mcp_server._mcp._err(msg.get("id") if isinstance(msg, dict) else None, -32603, f"Internal error: {exc}")
            raw = json.dumps(error_resp) + "\n"
        else:
            raw = out.getvalue()
        body_bytes = raw.encode("utf-8") if raw else b""
        try:
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
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("MCP HTTP: client disconnected during /mcp response")

    def _handle_message(self) -> None:
        auth = self._require_auth()
        if auth is None:
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
            state = mcp_server._http_sessions.get(session_id)
            if state is None:
                self._send_error_response(400, -32602, "Unknown sessionId")
                return
            with mcp_server._sse_lock:
                sse_queue = mcp_server._sse_clients.get(session_id)
        else:
            state = {
                "initialize_seen": False,
                "initialized": False,
                "strict_lifecycle": False,
                "trusted_run_id": f"http:{uuid.uuid4()}",
                "http_profile": mcp_server._http_profile,
                "http_delegate_auto_approve": mcp_server._http_profile in ("agent", "all"),
            }
        if sse_queue is not None:
            out = _SSEQueueOutput(sse_queue)
            state["sse_queue"] = sse_queue
        else:
            out = StringIO()
        if session_id:
            state["session_id"] = session_id
        state["http_profile"] = mcp_server._http_profile
        state["http_delegate_auto_approve"] = mcp_server._http_profile in ("agent", "all")
        self._apply_quimera_run_headers(state)
        self._apply_auth_context(state, auth)
        _set_mcp_state(out, state)

        try:
            mcp_server._mcp._process_message(msg, out=out, transport="http_mcp")
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
            try:
                self.send_response(500)
                self._send_cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
            except (BrokenPipeError, ConnectionResetError):
                _logger.debug("MCP HTTP: client disconnected during error response")
            return

        if isinstance(out, StringIO):
            raw = out.getvalue()
            if raw:
                body_bytes = raw.encode("utf-8")
                try:
                    self.send_response(200)
                    self._send_cors()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                except (BrokenPipeError, ConnectionResetError):
                    _logger.debug("MCP HTTP: client disconnected during message response")
                return

        try:
            self.send_response(202)
            self._send_cors()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("MCP HTTP: client disconnected during 202 response")

    def _send_error_response(
        self, status: int, code: int, message: str
    ) -> None:
        error_resp = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": message},
        }
        body_bytes = json.dumps(error_resp).encode("utf-8")
        try:
            self.send_response(status)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            _logger.debug("MCP HTTP: client disconnected during error response")


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
        port: int | None = None,
        allowed_tools: Iterable[str] | None = DEFAULT_HTTP_READ_ONLY_TOOLS,
        cors_origins: str | Iterable[str] | None = None,
        oauth: OAuthProvider | OAuthConfig | None = None,
    ) -> None:
        self._mcp = mcp_server
        self._mcp.set_allowed_tools(allowed_tools)
        self._http_profile = self._profile_name_for_allowed_tools(allowed_tools)
        self._oauth = self._resolve_oauth(oauth)
        self._cors_origins = self._normalize_cors_origins(cors_origins)
        self._host = host or os.environ.get(
            _QUIMERA_MCP_HTTP_HOST, _DEFAULT_HOST
        )
        # port=0 → SO escolhe porta livre (bind random); port=None → usa env/padrão.
        if port is None:
            env_port = os.environ.get(_QUIMERA_MCP_HTTP_PORT, "")
            self._port = int(env_port) if env_port else _DEFAULT_PORT
        else:
            self._port = port
        self._sse_clients: dict[str, queue.Queue] = {}
        self._http_sessions: dict[str, dict] = {}
        self._sse_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: Exception | None = None
        self._startup_lock = threading.Lock()
        self._startup_abandoned = threading.Event()


    @staticmethod
    def _resolve_oauth(oauth: OAuthProvider | OAuthConfig | None) -> OAuthProvider:
        """Normaliza o argumento ``oauth`` para um ``OAuthProvider``.

        ``None`` produz um provider desabilitado para usos embutidos/testes que
        injetam o transporte diretamente. O bootstrap da aplicação sempre passa
        um provider OAuth habilitado ao HTTP externo.
        """
        if isinstance(oauth, OAuthProvider):
            return oauth
        if isinstance(oauth, OAuthConfig):
            return OAuthProvider(oauth)
        return OAuthProvider(OAuthConfig())

    @property
    def oauth(self) -> OAuthProvider:
        """Authorization Server embutido (desabilitado quando não configurado)."""
        return self._oauth

    def connected_clients(self) -> list[ConnectedMCPClient]:
        """Retorna os clients atualmente conectados ao MCP HTTP.

        A visão é derivada das sessões MCP vivas, não do cadastro persistido de
        clients OAuth. Assim a UI distingue autorização registrada de conexão
        realmente ativa.
        """
        with self._sse_lock:
            sessions = list(self._http_sessions.items())

        clients: list[ConnectedMCPClient] = []
        for session_id, state in sessions:
            client_info = state.get("client_info") or {}
            clients.append(
                ConnectedMCPClient(
                    session_id=str(session_id),
                    client_id=str(state.get("oauth_client_id") or ""),
                    client_name=str(client_info.get("name") or ""),
                    scope=str(state.get("oauth_scope") or ""),
                    profile=str(state.get("http_profile") or ""),
                    initialized=bool(state.get("initialized")),
                    connected=True,
                    authorized=True,
                )
            )
        return clients

    def known_clients(self) -> list[ConnectedMCPClient]:
        """Retorna clients OAuth registrados, enriquecidos com sessões ativas.

        A UI usa essa visão para não fazer um client autorizado desaparecer
        entre requests ou durante uma reconexão. Quando há sessão ativa, os
        dados da sessão prevalecem sobre os metadados persistidos do client.
        """
        active = self.connected_clients()
        result: list[ConnectedMCPClient] = []
        active_by_id: dict[str, ConnectedMCPClient] = {}
        anonymous_sessions: list[ConnectedMCPClient] = []

        for client in active:
            if not client.client_id:
                anonymous_sessions.append(client)
                continue
            current = active_by_id.get(client.client_id)
            if current is None or (client.initialized and not current.initialized):
                active_by_id[client.client_id] = client

        result.extend(active_by_id.values())
        result.extend(anonymous_sessions)
        seen_ids = set(active_by_id)

        for client_id, oauth_client in self._oauth.clients.items():
            if client_id in seen_ids:
                active_client = active_by_id[client_id]
                if not active_client.client_name and oauth_client.client_name:
                    replacement = ConnectedMCPClient(
                        session_id=active_client.session_id,
                        client_id=active_client.client_id,
                        client_name=str(oauth_client.client_name),
                        scope=active_client.scope or str(oauth_client.scope or ""),
                        profile=active_client.profile,
                        initialized=active_client.initialized,
                        connected=True,
                        authorized=True,
                    )
                    result[result.index(active_client)] = replacement
                continue
            authorized = self._oauth.has_client_authorization(client_id)
            if oauth_client.dynamic and not authorized:
                continue
            result.append(
                ConnectedMCPClient(
                    session_id="",
                    client_id=str(client_id),
                    client_name=str(oauth_client.client_name or client_id),
                    scope=str(oauth_client.scope or ""),
                    profile="",
                    initialized=False,
                    connected=False,
                    authorized=authorized,
                )
            )
        return result

    def revoke_client_authorization(self, client_id: str) -> int:
        """Revoga o grant OAuth de um client e encerra suas sessões HTTP."""
        removed = self._oauth.revoke_client_authorization(client_id)
        queues_to_close: list[queue.Queue] = []
        with self._sse_lock:
            session_ids = [
                session_id
                for session_id, state in self._http_sessions.items()
                if str(state.get("oauth_client_id") or "") == client_id
            ]
            for session_id in session_ids:
                self._http_sessions.pop(session_id, None)
                sse_queue = self._sse_clients.pop(session_id, None)
                if sse_queue is not None:
                    queues_to_close.append(sse_queue)
        for sse_queue in queues_to_close:
            sse_queue.put(None)
        return removed + len(session_ids)

    def active_clients(self) -> list[ActiveMCPHTTPClient]:
        """Retorna snapshot das sessões MCP HTTP autenticadas atualmente."""
        with self._sse_lock:
            states = [dict(state) for state in self._http_sessions.values()]
        oauth_clients = self._oauth.clients
        result: list[ActiveMCPHTTPClient] = []
        for state in states:
            oauth_client_id = str(state.get("oauth_client_id") or "").strip()
            client_info = state.get("client_info")
            if not isinstance(client_info, dict):
                client_info = {}
            oauth_client = oauth_clients.get(oauth_client_id)
            oauth_client_name = ""
            if oauth_client is not None:
                oauth_client_name = str(
                    oauth_client.client_name or oauth_client.client_id or ""
                ).strip()
            result.append(
                ActiveMCPHTTPClient(
                    session_id=str(state.get("session_id") or "").strip(),
                    oauth_client_id=oauth_client_id,
                    oauth_client_name=oauth_client_name,
                    mcp_client_name=str(client_info.get("name") or "").strip(),
                    mcp_client_version=str(client_info.get("version") or "").strip(),
                    scope=str(state.get("oauth_scope") or "").strip(),
                    protocol_version=str(state.get("protocol_version") or "").strip(),
                )
            )
        return result

    def disabled_tools_for_profile(self, profile: str) -> frozenset[str]:
        """Tools a bloquear quando o escopo do token restringe o perfil do transporte.

        Args:
            profile: Nome do perfil derivado do escopo OAuth concedido.

        Returns:
            Conjunto de tools publicadas pelo transporte que ficam fora do
            perfil do token; vazio quando o escopo não restringe nada.
        """
        scope_tools = HTTP_TOOL_PROFILES.get(profile)
        if scope_tools is None:
            return frozenset()
        server_tools = self.allowed_tools
        if server_tools is None:
            try:
                server_tools = frozenset(self._mcp._executor.registry.names())
            except Exception:
                _logger.debug("MCP OAuth: registry indisponível para restringir perfil %r", profile)
                return frozenset()
        return frozenset(server_tools - scope_tools)

    @staticmethod
    def _profile_name_for_allowed_tools(allowed_tools: Iterable[str] | None) -> str:
        normalized = MCPServer._normalize_allowed_tools(allowed_tools)
        for name, tools in HTTP_TOOL_PROFILES.items():
            if normalized == tools:
                return name
        return "custom"

    @staticmethod
    def _normalize_cors_origins(
        cors_origins: str | Iterable[str] | None,
    ) -> frozenset[str]:
        if cors_origins is None:
            raw = os.environ.get(_QUIMERA_MCP_HTTP_CORS_ORIGINS, "http://127.0.0.1:8080")
            items: Iterable[str] = raw.split(",")
        elif isinstance(cors_origins, str):
            items = cors_origins.split(",")
        else:
            items = cors_origins
        normalized = frozenset(
            str(origin).strip() for origin in items if str(origin).strip()
        )
        return normalized or frozenset({"http://127.0.0.1:8080"})

    @property
    def cors_origins(self) -> frozenset[str]:
        """Origens CORS permitidas; ``{"http://127.0.0.1:8080"}`` padrão seguro."""
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
        try:
            server = _QuietThreadingHTTPServer(
                (self._host, self._port), _MCPHTTPRequestHandler
            )
        except Exception as exc:
            self._startup_error = exc
            self._ready_event.set()
            return
        with self._startup_lock:
            if self._startup_abandoned.is_set():
                server.server_close()
                self._ready_event.set()
                return
            self._httpd = server
        # Captura a porta real após o bind (relevante quando port=0 foi pedido).
        self._port = server.server_address[1]
        server.mcp_http_server = self
        self._mcp._start_background_flush()
        with self._startup_lock:
            if self._startup_abandoned.is_set():
                self._mcp._stop_background_flush()
                server.server_close()
                self._httpd = None
                self._ready_event.set()
                return
        self._ready_event.set()
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
            self._httpd = None
            _logger.info("MCP HTTP+SSE server stopped")

    def start_background(self) -> None:
        """Inicia o servidor HTTP em uma thread daemon e retorna após o bind."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("MCP HTTP server já está em execução")
        if self._host not in ("127.0.0.1", "localhost", ""):
            _logger.warning(
                "MCP HTTP server sem TLS — tráfego não criptografado em rede: %s",
                self._host,
            )
        self._ready_event.clear()
        self._startup_abandoned.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        if not self._ready_event.wait(timeout=10):
            with self._startup_lock:
                self._startup_abandoned.set()
            self._thread.join(timeout=1)
            raise TimeoutError("MCP HTTP server não ficou pronto em 10 segundos")
        if self._startup_error is not None:
            self._thread.join(timeout=1)
            self._thread = None
            raise RuntimeError("Não foi possível iniciar o servidor MCP HTTP") from self._startup_error

    def shutdown(self) -> None:
        """Para o servidor HTTP e sinaliza todas as conexões SSE."""
        self._mcp._stop_background_flush()
        self._mcp.shutdown()
        if self._httpd:
            self._httpd.shutdown()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                _logger.warning("Thread do servidor MCP HTTP não encerrou em 5 segundos")
            else:
                self._thread = None
        with self._sse_lock:
            for q in self._sse_clients.values():
                q.put_nowait(None)
            self._sse_clients.clear()


def create_server(
    mcp_server: MCPServer,
    host: str = "",
    port: int | None = None,
    allowed_tools: Iterable[str] | None = DEFAULT_HTTP_READ_ONLY_TOOLS,
    cors_origins: str | Iterable[str] | None = None,
    oauth: OAuthProvider | OAuthConfig | None = None,
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
        oauth: ``OAuthProvider`` ou ``OAuthConfig`` do Authorization Server
            embutido. O bootstrap do MCP HTTP externo fornece OAuth habilitado.

    Returns:
        MCP_HTTPServer configurado mas não iniciado.
    """
    return MCP_HTTPServer(
        mcp_server,
        host=host,
        port=port,
        allowed_tools=allowed_tools,
        cors_origins=cors_origins,
        oauth=oauth,
    )
