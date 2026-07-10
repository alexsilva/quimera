"""MCP Client — conecta a servidores MCP externos e expõe suas tools como handlers locais.

Suporta os transportes:
  - remote:  ``https://mcp.atlassian.com/v1/sse`` (atalho para ``npx -y mcp-remote``)
  - stdio:   ``python -m algum_servidor_mcp``
  - socket:  ``/tmp/meu-mcp.sock``
  - http:    ``http://localhost:3100/mcp``

Uso típico via CLI::

    quimera --mcp-client 'atlassian=remote:https://mcp.atlassian.com/v1/sse'
    quimera --mcp-client wiki=http://localhost:3100/mcp

O bridge conecta, faz handshake ``initialize``, descobre tools via ``tools/list``
e registra cada uma com prefixo ``<nome>_`` no ``ToolRegistry`` do Quimera.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from quimera.runtime.models import ToolCall, ToolResult

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPClientRuntime:
    """Estado retornado pela inicialização de MCP clients externos."""

    enabled: bool
    bridge: "MCPClientBridge | None" = None
    specs: tuple[str, ...] = ()
    env_overrides: dict[str, dict[str, str]] | None = None

# ── Helpers ──────────────────────────────────────────────────────────────


def _build_request(
    method: str, params: dict | None = None, msg_id: str | int | None = None
) -> dict:
    request: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        request["params"] = params
    if msg_id is not None:
        request["id"] = msg_id
    return request


def _read_line(stream: IO[str]) -> str | None:
    line = stream.readline()
    if not line:
        return None
    return line.rstrip("\n").rstrip("\r")


def _read_response(
    stream: IO[str], request_id: str | int, timeout: float = 30.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = _read_line(stream)
        if line is None:
            raise ConnectionError("MCP client: conexão fechada pelo servidor")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if "id" not in msg:
            continue
        if msg.get("id") == request_id:
            return msg
    raise TimeoutError(
        f"MCP client: timeout aguardando resposta para {request_id}"
    )


# ── Transporte abstrato ──────────────────────────────────────────────────


class MCPTransport(ABC):
    """Interface para transporte MCP bidirecional."""

    @abstractmethod
    def connect(self) -> tuple[IO[str], IO[str]]:
        """Estabelece conexão e retorna (reader, writer)."""

    @abstractmethod
    def disconnect(self) -> None:
        """Fecha a conexão."""

    @property
    @abstractmethod
    def transport_type(self) -> str:
        """Identificador do tipo de transporte (stdio, socket, http)."""


class StdioMCPTransport(MCPTransport):
    """Transporte via subprocesso (stdio)."""

    _STDERR_NOISE_PATTERNS = (
        "[Local→Remote]",
        "[Remote→Local]",
        '"jsonrpc"',
        '"method"',
        '"params"',
        '"protocolVersion"',
        '"capabilities"',
        '"clientInfo"',
        '"name"',
        '"version"',
        "{",
        "}",
    )

    _STDERR_INFO_PATTERNS = (
        "Please authorize this client by visiting:",
        "Browser opened automatically.",
        "Connected to remote server",
        "Proxy established successfully",
        "Local STDIO server running",
    )

    _STDERR_EXPECTED_PATTERNS = (
        "Missing sessionId parameter",
        "falling-back-to-alternate-transport",
        "Recursively reconnecting for reason: falling-back-to-alternate-transport",
    )

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        name: str | None = None,
    ) -> None:
        self._command = command
        self._env = env
        self._name = name
        self._process: subprocess.Popen | None = None
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._stderr_debug = os.environ.get("QUIMERA_MCP_STDIO_DEBUG", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._stderr_printed: set[str] = set()

    def connect(self) -> tuple[IO[str], IO[str]]:
        proc_env = None
        if self._env:
            proc_env = {**os.environ, **self._env}
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=proc_env,
            start_new_session=True,
        )
        self._start_stderr_pump()
        return self._process.stdout, self._process.stdin

    def _start_stderr_pump(self) -> None:
        if not self._process or not self._process.stderr:
            return

        def pump() -> None:
            assert self._process is not None
            assert self._process.stderr is not None
            for line in self._process.stderr:
                text = line.rstrip("\n").rstrip("\r")
                if not text:
                    continue
                with self._stderr_lock:
                    self._stderr_lines.append(text)
                    if len(self._stderr_lines) > 200:
                        self._stderr_lines = self._stderr_lines[-200:]
                self._print_stderr_line(text)

        threading.Thread(target=pump, daemon=True).start()

    def _strip_mcp_remote_prefix(self, text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("[") or "]" not in stripped:
            return stripped
        _, rest = stripped.split("]", 1)
        rest = rest.strip()
        if rest.startswith("[") and "]" in rest:
            _, rest = rest.split("]", 1)
            rest = rest.strip()
        return rest or stripped

    def _print_stderr_line(self, text: str) -> None:
        if self._stderr_debug:
            print(f"  MCP stdio stderr: {text}", file=sys.stderr)
            return

        clean = self._strip_mcp_remote_prefix(text)
        if not clean:
            return

        if clean in self._STDERR_NOISE_PATTERNS:
            return
        if any(pattern in clean for pattern in self._STDERR_NOISE_PATTERNS):
            return
        if any(pattern in clean for pattern in self._STDERR_EXPECTED_PATTERNS):
            _logger.debug("MCP stdio expected stderr: %s", clean)
            return

        if clean.startswith("http://") or clean.startswith("https://"):
            self._print_auth_prompt(clean)
            return

        label = f" '{self._name}'" if self._name else ""
        if any(token in clean.lower() for token in ("error", "failed", "exception", "eaddrinuse", "unauthorized", "forbidden")):
            rendered = f"MCP stdio erro{label}: {clean}"
        elif any(pattern in clean for pattern in self._STDERR_INFO_PATTERNS):
            # Sinais de progresso do mcp-remote (conexão estabelecida, proxy,
            # servidor STDIO local). A camada Quimera já anuncia
            # "conectando..."/"conectado com sucesso" por conexão, então estas
            # linhas seriam redundantes no console — ficam apenas no log.
            _logger.debug("MCP stdio progresso%s: %s", label, clean)
            return
        else:
            _logger.debug("MCP stdio stderr: %s", clean)
            return

        if rendered in self._stderr_printed:
            return
        self._stderr_printed.add(rendered)
        print(f"  {rendered}", file=sys.stderr)

    def _print_auth_prompt(self, url: str) -> None:
        """Exibe o link de autorização OAuth como um bloco destacado.

        O ``mcp-remote`` emite a URL de autorização no stderr; em vez de deixá-la
        perdida entre linhas de diagnóstico, apresentamos um bloco claro com o
        nome da conexão e o estado de espera pelo navegador.
        """
        if url in self._stderr_printed:
            return
        self._stderr_printed.add(url)
        label = f" '{self._name}'" if self._name else ""
        bar = "─" * 64
        lines = [
            "",
            f"  {bar}",
            f"  🔓 Autorização MCP necessária — conexão{label}",
            "     Abra este link no navegador para autorizar o acesso:",
            f"     {url}",
            "     Aguardando confirmação no navegador…",
            f"  {bar}",
            "",
        ]
        print("\n".join(lines), file=sys.stderr)

    def stderr_tail(self, limit: int = 4000) -> str:
        """Retorna stderr do processo quando ele já encerrou.

        Não lemos stderr de processo vivo para evitar bloquear o handshake MCP.
        """
        if not self._process or not self._process.stderr:
            return ""
        with self._stderr_lock:
            stderr = "\n".join(self._stderr_lines)
        return stderr.strip()[-limit:]

    def command_label(self) -> str:
        """Comando formatado para diagnóstico sem expor ambiente."""
        return " ".join(shlex.quote(part) for part in self._command)

    def disconnect(self) -> None:
        if self._process:
            try:
                os.killpg(self._process.pid, 15)
            except Exception:
                self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, 9)
                except Exception:
                    self._process.kill()
            self._process = None

    @property
    def transport_type(self) -> str:
        return "stdio"


class SocketMCPTransport(MCPTransport):
    """Transporte via socket Unix."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._sock: socket.socket | None = None

    def connect(self) -> tuple[IO[str], IO[str]]:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self._socket_path)
        reader = self._sock.makefile("r", encoding="utf-8", errors="replace")
        writer = self._sock.makefile("w", encoding="utf-8")
        return reader, writer

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def transport_type(self) -> str:
        return "socket"


class HttpMCPTransport(MCPTransport):
    """Transporte via HTTP (Streamable MCP)."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._session_id: str | None = None
        self._session_lock = threading.Lock()

    def connect(self) -> tuple[IO[str], IO[str]]:
        return _HttpReaderWriter(self), _HttpWriterDummy()

    def disconnect(self) -> None:
        with self._session_lock:
            sid = self._session_id
            self._session_id = None
        if sid:
            try:
                self._http_request("DELETE", headers={"MCP-Session-Id": sid})
            except Exception:
                pass

    @property
    def transport_type(self) -> str:
        return "http"

    def send_mcp_request(
        self, method: str, params: dict | None = None
    ) -> dict:
        msg_id = str(uuid.uuid4())
        body = _build_request(method, params, msg_id)
        resp_data = self._http_request(
            "POST", headers={"Content-Type": "application/json"}, data=body
        )
        if resp_data:
            return resp_data
        return {}

    def http_initialize(self) -> dict:
        body = _build_request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "quimera-mcp-client",
                    "version": "0.1.0",
                },
            },
            "init-1",
        )
        resp_data = self._http_request(
            "POST",
            headers={"Content-Type": "application/json"},
            data=body,
            expect_session=True,
        )
        if resp_data:
            return resp_data
        return {}

    def _http_request(
        self,
        method: str,
        headers: dict | None = None,
        data: dict | None = None,
        expect_session: bool = False,
    ) -> dict:
        import urllib.error
        import urllib.request

        req_data = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(
            self._url, data=req_data, method=method
        )
        req.add_header("MCP-Protocol-Version", "2025-11-25")
        with self._session_lock:
            if self._session_id:
                req.add_header("MCP-Session-Id", self._session_id)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        if headers:
            for k, v in headers.items():
                if k.lower() not in ("content-type",):
                    req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if expect_session:
                    sid = resp.headers.get("MCP-Session-Id")
                    if sid:
                        with self._session_lock:
                            self._session_id = sid
                body_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()

        if body_bytes:
            try:
                return json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}


class _HttpReaderWriter:
    def __init__(self, transport: HttpMCPTransport) -> None:
        self._transport = transport


class _HttpWriterDummy:
    """Placeholder — HttpMCPTransport usa request/response direto."""


# ── Sessão MCP Cliente ──────────────────────────────────────────────────


class MCPClientSession:
    """Gerencia uma sessão com um servidor MCP externo."""

    def __init__(self, transport: MCPTransport, name: str = "external") -> None:
        self._transport = transport
        self._name = name
        self._reader: IO[str] | None = None
        self._writer: IO[str] | None = None
        self._lock = threading.Lock()
        self._server_info: dict = {}
        self._protocol_version: str = ""
        self._seq = 0
        self._connected = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def server_info(self) -> dict:
        return dict(self._server_info)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def transport_type(self) -> str:
        return self._transport.transport_type

    def connect(self) -> None:
        self._reader, self._writer = self._transport.connect()
        self._connected = True

        try:
            if isinstance(self._transport, HttpMCPTransport):
                result = self._transport.http_initialize()
            else:
                result = self._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "quimera-mcp-client",
                            "version": "0.1.0",
                        },
                    },
                )
        except Exception as exc:
            self._connected = False
            if isinstance(self._transport, StdioMCPTransport):
                stderr = self._transport.stderr_tail()
                command = self._transport.command_label()
                if stderr:
                    raise ConnectionError(
                        f"{exc}; comando: {command}; stderr: {stderr}"
                    ) from exc
                raise ConnectionError(f"{exc}; comando: {command}") from exc
            raise

        self._server_info = result.get("serverInfo", result)
        self._protocol_version = result.get("protocolVersion", "")
        self._send_notification("notifications/initialized")
        _logger.info(
            "MCP client '%s' conectado a %s v%s",
            self._name,
            self._server_info.get("name", "?"),
            self._protocol_version,
        )

    def disconnect(self) -> None:
        self._connected = False
        self._transport.disconnect()

    def list_tools(self) -> list[dict]:
        result = self._send_request("tools/list", {})
        raw = result.get("tools", []) if isinstance(result, dict) else []
        return list(raw)

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._send_request("tools/call", {"name": name, "arguments": arguments})

    def send_ping(self) -> dict:
        return self._send_request("ping", {})

    def _next_id(self) -> str:
        self._seq += 1
        return f"mcp-{self._seq}"

    def _send_request(self, method: str, params: dict) -> dict:
        if isinstance(self._transport, HttpMCPTransport):
            resp = self._transport.send_mcp_request(method, params)
            if isinstance(resp, dict):
                if "error" in resp:
                    err = resp["error"]
                    raise RuntimeError(
                        f"MCP error {err.get('code', '?')}: {err.get('message', '?')}"
                    )
                return resp.get("result") or {}
            return {}

        msg_id = self._next_id()
        body = _build_request(method, params, msg_id)

        with self._lock:
            if self._writer is None:
                raise ConnectionError("MCP client: não conectado")
            line = json.dumps(body, ensure_ascii=False) + "\n"
            self._writer.write(line)
            self._writer.flush()
            resp = _read_response(self._reader, msg_id)

        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"MCP error {err.get('code', '?')}: {err.get('message', '?')}"
            )
        return resp.get("result") or {}

    def _send_notification(
        self, method: str, params: dict | None = None
    ) -> None:
        if isinstance(self._transport, HttpMCPTransport):
            return
        body = _build_request(method, params, msg_id=None)
        with self._lock:
            if self._writer is None:
                return
            line = json.dumps(body, ensure_ascii=False) + "\n"
            self._writer.write(line)
            self._writer.flush()


# ── Bridge ───────────────────────────────────────────────────────────────


class MCPClientBridge:
    """Bridge entre servidores MCP externos e o runtime Quimera.

    Conecta a servidores MCP externos configurados, descobre suas tools
    via ``tools/list`` e registra handlers no ``ToolRegistry`` que
    traduzem chamadas locais em chamadas remotas.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MCPClientSession] = {}
        self._lock = threading.Lock()
        self._started = False
        self._schemas: list[dict] = []
        self._schema_lock = threading.Lock()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def sessions(self) -> dict[str, MCPClientSession]:
        return dict(self._sessions)

    def add_connection(
        self, name: str, transport: MCPTransport
    ) -> MCPClientSession:
        session = MCPClientSession(transport, name=name)
        session.connect()
        with self._lock:
            self._sessions[name] = session
            self._started = True
        _logger.info(
            "MCP bridge: '%s' conectado (%s)", name, transport.transport_type
        )
        return session

    def register_handlers(self, registry) -> list[str]:
        """Descobre tools de todas as sessões e registra handlers no ToolRegistry.

        Returns:
            Lista de nomes de tools registradas.
        """
        registered = []
        all_schemas: list[dict] = []

        for session_name, session in self._sessions.items():
            try:
                tools = session.list_tools()
            except Exception as exc:
                _logger.warning(
                    "MCP bridge '%s': falha ao listar tools: %s",
                    session_name,
                    exc,
                )
                continue

            effective_prefix = f"{session_name}_"
            for tool in tools:
                tool_name = tool.get("name", "")
                if not tool_name:
                    continue

                local_name = f"{effective_prefix}{tool_name}"
                description = tool.get("description", "")
                input_schema = tool.get(
                    "inputSchema", {"type": "object", "properties": {}}
                )

                handler = self._make_handler(
                    session, tool_name, description, input_schema
                )
                registry.register(local_name, handler)
                registered.append(local_name)

                openai_schema = {
                    "type": "function",
                    "function": {
                        "name": local_name,
                        "description": description,
                        "parameters": input_schema,
                    },
                }
                all_schemas.append(openai_schema)

                _logger.debug(
                    "MCP bridge '%s': registrou '%s' <- '%s'",
                    session_name,
                    local_name,
                    tool_name,
                )

        with self._schema_lock:
            self._schemas = all_schemas

        return registered

    def get_schemas(self) -> list[dict]:
        """Retorna schemas OpenAI das tools bridgeadas."""
        with self._schema_lock:
            return list(self._schemas)

    @staticmethod
    def _make_handler(
        session: MCPClientSession,
        remote_tool_name: str,
        description: str,
        input_schema: dict,
    ) -> Callable[[ToolCall], ToolResult]:
        def handler(call: ToolCall) -> ToolResult:
            start = time.monotonic()
            try:
                result = session.call_tool(remote_tool_name, call.arguments)
                duration = int((time.monotonic() - start) * 1000)

                content_parts = result.get("content", [])
                text_parts = []
                for part in content_parts:
                    if isinstance(part, dict):
                        text_parts.append(part.get("text", str(part)))
                    else:
                        text_parts.append(str(part))

                is_error = result.get("isError", False)
                return ToolResult(
                    ok=not is_error,
                    tool_name=call.name,
                    content="\n".join(text_parts),
                    error=result.get("error") if is_error else None,
                    duration_ms=duration,
                    data=result,
                )
            except Exception as exc:
                duration = int((time.monotonic() - start) * 1000)
                return ToolResult(
                    ok=False,
                    tool_name=call.name,
                    error=str(exc),
                    duration_ms=duration,
                )

        return handler

    def shutdown(self) -> None:
        for name, session in self._sessions.items():
            try:
                session.disconnect()
                _logger.info("MCP bridge: '%s' desconectado", name)
            except Exception as exc:
                _logger.warning(
                    "MCP bridge '%s': erro ao desconectar: %s", name, exc
                )
        self._sessions.clear()
        self._started = False
        with self._schema_lock:
            self._schemas.clear()


# ── Factory ──────────────────────────────────────────────────────────────


DEFAULT_MCP_REMOTE_RUNNER = "npx -y mcp-remote"


def build_mcp_remote_command(endpoint: str) -> list[str]:
    """Expande o atalho ``remote:`` para o comando do ``mcp-remote``.

    ``endpoint`` é a URL do servidor remoto, opcionalmente seguida de argumentos
    extras do ``mcp-remote`` (ex.: ``--header ...``). O runner padrão é
    ``npx -y mcp-remote`` e pode ser sobrescrito via ``QUIMERA_MCP_REMOTE_CMD``
    (útil para fixar versão do pacote ou usar outro executor).
    """
    tail = shlex.split(endpoint or "")
    if not tail:
        return []
    runner = os.environ.get("QUIMERA_MCP_REMOTE_CMD", DEFAULT_MCP_REMOTE_RUNNER).strip()
    return shlex.split(runner) + tail


def parse_mcp_client_spec(
    spec: str,
    env_overrides: dict[str, dict[str, str]] | None = None,
) -> tuple[str, MCPTransport]:
    """Interpreta uma especificação ``--mcp-client``.

    Formatos aceitos:

    * ``nome=remote:https://host/sse`` — atalho para servidores OAuth remotos;
      expande para ``npx -y mcp-remote https://host/sse``
    * ``nome=stdio:comando arg1 arg2`` — subprocesso
    * ``nome=socket:/path/to/sock`` — socket Unix
    * ``nome=http://host:port/path`` — HTTP Streamable MCP
    * ``nome=https://host:port/path`` — HTTPS Streamable MCP

    ``env_overrides`` é opcional: mapeia nome_da_conexão -> {VAR: valor, ...}.
    """
    if "=" not in spec:
        raise ValueError(
            f"Formato inválido para --mcp-client: {spec!r} "
            f"(esperado nome=transporte:...)"
        )

    name, rest = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"Nome vazio em --mcp-client: {spec!r}")

    rest = rest.strip()

    env_override = (env_overrides or {}).get(name, {})

    if rest.startswith("http://") or rest.startswith("https://"):
        token = env_override.get("MCP_TOKEN") or env_override.get("token")
        return name, HttpMCPTransport(rest, token=token)

    if ":" not in rest:
        raise ValueError(
            f"Formato inválido para --mcp-client: {spec!r}. "
            f"Para stdio, use: nome=stdio:cmd arg1 arg2. "
            f"Para socket, use: nome=socket:/path/to/sock"
        )

    transport_type, endpoint = rest.split(":", 1)
    transport_type = transport_type.strip().lower()
    endpoint = endpoint.strip()

    if transport_type == "stdio":
        args = shlex.split(endpoint)
        return name, StdioMCPTransport(args, env=env_override or None, name=name)
    if transport_type == "remote":
        command = build_mcp_remote_command(endpoint)
        if not command:
            raise ValueError(
                f"Transporte remote exige uma URL em --mcp-client: {spec!r}. "
                f"Ex: nome=remote:https://mcp.exemplo.com/sse"
            )
        return name, StdioMCPTransport(command, env=env_override or None, name=name)
    if transport_type == "socket":
        return name, SocketMCPTransport(endpoint)

    raise ValueError(
        f"Transporte desconhecido em --mcp-client: {transport_type!r}. "
        f"Esperado: stdio, remote, socket, http, https"
    )


def build_bridge_from_cli(
    specs: list[str],
    env_overrides: dict[str, dict[str, str]] | None = None,
) -> MCPClientBridge:
    """Constrói e conecta um MCPClientBridge a partir de especificações CLI."""
    bridge = MCPClientBridge()
    for spec in specs:
        display_name = spec
        try:
            name, transport = parse_mcp_client_spec(spec, env_overrides)
            display_name = name
            print(
                f"  MCP client '{name}': conectando via {transport.transport_type}...",
                file=sys.stderr,
            )
            bridge.add_connection(name, transport)
            _logger.info(
                "MCP client '%s' conectado via %s",
                name, transport.transport_type,
            )
            print(
                f"  MCP client '{name}': conectado com sucesso",
                file=sys.stderr,
            )
        except Exception as exc:
            _logger.error(
                "Falha ao conectar MCP client '%s': %s", display_name, exc
            )
            print(
                f"  MCP client '{display_name}': FALHA — {exc}",
                file=sys.stderr,
            )
    return bridge


def parse_mcp_client_env_specs(
    specs: list[str] | tuple[str, ...] | None,
) -> dict[str, dict[str, str]] | None:
    """Normaliza specs ``--mcp-client-env`` para overrides por conexão.

    Formato aceito: ``nome=KEY=valor,KEY2=valor2``.
    Entradas inválidas são ignoradas com warning, preservando o bootstrap.
    """
    if not specs:
        return None
    env_overrides: dict[str, dict[str, str]] = {}
    for spec in specs:
        if "=" not in spec:
            _logger.warning("Formato inválido para --mcp-client-env: %r", spec)
            continue
        conn_name, rest = spec.split("=", 1)
        conn_name = conn_name.strip()
        pairs: dict[str, str] = {}
        for part in rest.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k.strip()] = v.strip()
        if conn_name and pairs:
            env_overrides[conn_name] = pairs
    return env_overrides or None


def _spec_name(spec: str) -> str:
    """Extrai o nome da conexão de uma spec ``nome=...``."""
    return spec.split("=", 1)[0].strip() if "=" in spec else spec.strip()


def merge_specs_by_name(
    existing: list[str] | None, incoming: list[str] | None
) -> list[str]:
    """Combina specs ``nome=...`` mantendo unicidade por nome de conexão.

    Uma spec de ``incoming`` com nome já presente em ``existing`` substitui a
    anterior (mesma conexão reconfigurada); nomes novos são anexados ao final.
    A ordem de ``existing`` é preservada.
    """
    merged = list(existing or [])
    index = {_spec_name(spec): pos for pos, spec in enumerate(merged)}
    for spec in incoming or []:
        name = _spec_name(spec)
        if name in index:
            merged[index[name]] = spec
        else:
            index[name] = len(merged)
            merged.append(spec)
    return merged


def start_mcp_clients(
    *,
    cli_specs: list[str] | None,
    cli_env_specs: list[str] | None,
    config: Any,
) -> MCPClientRuntime:
    """Inicializa MCP clients externos e publica o bridge global.

    Deve rodar antes da criação do ``QuimeraApp`` para que o ``ToolExecutor``
    registre handlers das tools externas durante o bootstrap.

    Specs vindas da CLI são combinadas às persistidas por nome de conexão: uma
    conexão nova é adicionada às já existentes, enquanto uma conexão de mesmo
    nome é reconfigurada (substituída), nunca duplicada.
    """
    persisted_specs = getattr(config, "mcp_clients", None)
    persisted_env = getattr(config, "mcp_client_env", None)

    if cli_specs:
        specs = merge_specs_by_name(persisted_specs, cli_specs)
        env_specs = merge_specs_by_name(persisted_env, cli_env_specs)
    else:
        specs = persisted_specs
        env_specs = persisted_env

    if not specs:
        return MCPClientRuntime(enabled=False)

    env_overrides = parse_mcp_client_env_specs(env_specs)
    bridge = build_bridge_from_cli(specs, env_overrides=env_overrides)
    if bridge and bridge.started:
        from quimera.runtime.drivers.tool_schemas import set_bridge_schemas
        from quimera.runtime.tools.mcp_clients import set_bridge as set_mcp_client_bridge

        set_mcp_client_bridge(bridge)
        schemas = bridge.get_schemas()
        if schemas:
            set_bridge_schemas(schemas)

    if cli_specs:
        config.set_mcp_clients(specs)
        config.set_mcp_client_env(env_specs)

    return MCPClientRuntime(
        enabled=bool(bridge and bridge.started),
        bridge=bridge,
        specs=tuple(specs),
        env_overrides=env_overrides,
    )
