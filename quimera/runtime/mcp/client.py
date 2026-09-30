"""MCP Client — conecta a servidores MCP externos e expõe suas tools como handlers locais.

Suporta os transportes:
  - remote:  ``https://mcp.atlassian.com/v1/mcp`` (atalho para uma versão testada de ``mcp-remote``)
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
import re
import shlex
import socket
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from quimera import process_factory as subprocess
from quimera.environment import build_env_vars
from quimera.sandbox.state import is_sandbox_enabled, wrap_subprocess_cmd
from quimera.runtime.mcp.remote_credentials import (
    migrate_legacy_remote_credentials,
    remote_config_dir,
)
from quimera.runtime.models import ToolCall, ToolResult

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPClientRuntime:
    """Estado retornado pela inicialização de MCP clients externos."""

    enabled: bool
    bridge: "MCPClientBridge | None" = None
    specs: tuple[str, ...] = ()
    env_overrides: dict[str, dict[str, str]] | None = None


class MCPConnectionPhase:
    """Fases do ciclo de vida de uma conexão MCP client."""

    PENDING = "pending"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class MCPConnectionState:
    """Estado observável de uma conexão MCP client, para UI e diagnóstico.

    O bridge publica um snapshot imutável por conexão; ``detail`` carrega o
    erro da última falha ou um aviso operacional (ex.: autorização OAuth
    pendente no navegador) enquanto o handshake está em andamento.
    """

    name: str
    transport: str = ""
    phase: str = MCPConnectionPhase.PENDING
    detail: str = ""
    tools: int = 0
    updated_at: float = 0.0
    #: URL de autorização OAuth que o servidor pediu para abrir no navegador;
    #: vazia quando não há autorização pendente.
    auth_url: str = ""

    @property
    def connected(self) -> bool:
        return self.phase == MCPConnectionPhase.CONNECTED

    @property
    def auth_pending(self) -> bool:
        return bool(self.auth_url)

    @property
    def in_progress(self) -> bool:
        return self.phase in {MCPConnectionPhase.PENDING, MCPConnectionPhase.CONNECTING}

    @property
    def failed(self) -> bool:
        return self.phase == MCPConnectionPhase.FAILED


MCPNoticeCallback = Callable[[str, str], None]


class MCPConnectError(ConnectionError):
    """Falha de handshake com um servidor MCP externo.

    ``str(exc)`` é um resumo de uma linha, apresentável no bloco de status e
    no MCP Hub. O comando e o stderr do processo ficam em ``diagnostics`` e
    vão apenas para o log do app — nunca para o chat.
    """

    def __init__(
        self,
        reason: str,
        *,
        name: str = "",
        command: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.name = name
        self.command = command
        self.stderr = stderr

    @property
    def diagnostics(self) -> str:
        """Texto completo para o log: motivo, comando e stderr do processo."""
        parts = [self.reason]
        if self.command:
            parts.append(f"comando: {self.command}")
        if self.stderr:
            parts.append(f"stderr:\n{self.stderr}")
        return "\n".join(parts)


class MCPConnectSuperseded(MCPConnectError):
    """Handshake abandonado porque outra tentativa da mesma conexão começou.

    Um reconectar (ou desconectar) enquanto o handshake anterior ainda espera
    — por exemplo, uma autorização OAuth nunca concluída — encerra o
    transporte antigo. A thread daquela tentativa recebe esta exceção e não
    publica estado, para não sobrescrever o progresso da tentativa nova.
    """


_STDERR_PREFIX_RE = re.compile(r"^(?:\s*\[[^\]]*\])+\s*")
_STDERR_SUMMARY_PRIORITY = (
    "fatal error",
    "error:",
    "erofs",
    "eacces",
    "enoent",
    "unauthorized",
    "forbidden",
    "error",
    "failed",
    "exception",
)
_READ_ONLY_FS_MARKERS = ("erofs", "read-only file system")


def shorten_text(text: object, limit: int) -> str:
    """Colapsa espaços e corta em ``limit`` caracteres com reticências."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)].rstrip() + "…"


def summarize_stderr(stderr: str, *, limit: int = 160) -> str:
    """Resume o stderr de um servidor stdio em uma única linha apresentável.

    Prefere a última linha que nomeia um erro (``Fatal error``, ``Error:``,
    códigos ``EROFS``/``EACCES``…) e ignora frames de stack trace e JSON. O
    texto completo continua disponível para o log; este resumo é o que o
    bloco de status e o MCP Hub exibem.
    """
    lines: list[str] = []
    for raw in str(stderr or "").splitlines():
        clean = _STDERR_PREFIX_RE.sub("", raw).strip()
        if not clean:
            continue
        if clean.startswith("at ") or clean.startswith(("{", "}", "[")):
            continue
        lines.append(clean)
    if not lines:
        return ""
    lowered = [line.lower() for line in lines]
    for pattern in _STDERR_SUMMARY_PRIORITY:
        for index in range(len(lines) - 1, -1, -1):
            if pattern in lowered[index]:
                return shorten_text(lines[index], limit)
    return shorten_text(lines[-1], limit)


def _looks_like_read_only_fs(stderr: str) -> bool:
    lowered = str(stderr or "").lower()
    return any(marker in lowered for marker in _READ_ONLY_FS_MARKERS)


def _uses_mcp_remote(command: list[str]) -> bool:
    return any("mcp-remote" in str(part) for part in command)


def _ensure_directory(path) -> str:
    """Cria ``path`` (0700) se necessário; o bwrap só monta origens existentes."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        _logger.debug("MCP stdio: não foi possível criar %s: %s", path, exc)
    return str(path)


def _sandbox_enabled(workspace) -> bool:
    if workspace is None:
        return False
    try:
        return bool(is_sandbox_enabled(workspace))
    except Exception:
        # Config inválida: wrap_subprocess_cmd falha fechado logo em seguida.
        return False

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

    def set_notice_callback(self, callback: MCPNoticeCallback | None) -> None:
        """Registra destino para avisos operacionais (``auth``/``error``).

        Transportes sem diagnóstico próprio ignoram o callback. Quando
        definido, o transporte deixa de escrever no stderr — a UI é quem
        apresenta o aviso.
        """
        return None


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
        workspace=None,
    ) -> None:
        self._command = command
        self._env = env
        self._name = name
        self.workspace = workspace
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
        self._stderr_thread: threading.Thread | None = None
        self._sandboxed = False
        self._notice_callback: MCPNoticeCallback | None = None

    def set_notice_callback(self, callback: MCPNoticeCallback | None) -> None:
        self._notice_callback = callback

    def sandbox_rw_paths(self) -> list[str]:
        """Diretórios fora do workspace que o servidor precisa escrever no sandbox.

        O ``mcp-remote`` guarda tokens OAuth e lockfiles em
        ``MCP_REMOTE_CONFIG_DIR`` (padrão ``~/.mcp-auth``); sem essa exceção o
        ``$HOME`` somente leitura do sandbox derruba o handshake com ``EROFS``.
        O diretório é criado aqui porque o bwrap só monta origens existentes.
        """
        if not _uses_mcp_remote(self._command):
            return []
        return [_ensure_directory(remote_config_dir(self._env))]

    def _emit_notice(self, kind: str, text: str) -> bool:
        """Entrega o aviso ao callback registrado; False se não houver."""
        callback = self._notice_callback
        if callback is None:
            return False
        try:
            callback(kind, text)
        except Exception:
            _logger.debug("MCP stdio: callback de aviso falhou", exc_info=True)
        return True

    def connect(self) -> tuple[IO[str], IO[str]]:
        proc_env = build_env_vars(
            os.environ,
            workspace=self.workspace,
            extra_env=self._env,
        )
        command = list(self._command)
        if self.workspace is not None:
            self._sandboxed = _sandbox_enabled(self.workspace)
            extra_rw_paths = self.sandbox_rw_paths() if self._sandboxed else []
            command = wrap_subprocess_cmd(
                self.workspace,
                str(self.workspace.cwd),
                command,
                extra_rw_paths=extra_rw_paths,
                die_with_parent=True,
            )
        self._process = subprocess.Popen(
            command,
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

        self._stderr_thread = threading.Thread(target=pump, daemon=True)
        self._stderr_thread.start()

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
        # Diagnóstico do processo vai para o log do app; o chat só recebe o
        # resumo da falha quando o handshake termina (ver describe_failure).
        _logger.info("%s", rendered)
        if self._emit_notice("error", clean):
            return
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
        if self._emit_notice("auth", url):
            return
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

    def _drain_stderr(self, timeout: float = 0.5) -> None:
        """Espera (limitado) o pump consumir o stderr restante de um processo que caiu."""
        thread = self._stderr_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def describe_failure(self, exc: BaseException, *, name: str = "") -> "MCPConnectError":
        """Converte a exceção do handshake em ``MCPConnectError``.

        O motivo apresentável combina a exceção com a linha mais informativa do
        stderr (ex.: ``Fatal error: … EROFS``) e, quando o sandbox do workspace
        está ativo e o erro é de filesystem somente leitura, aponta a causa.
        Comando e stderr completos ficam só em ``diagnostics`` para o log.
        """
        self._drain_stderr()
        stderr = self.stderr_tail()
        reason = str(exc).strip() or exc.__class__.__name__
        if self._sandboxed and _looks_like_read_only_fs(stderr):
            # A causa acionável vem antes do trecho do stderr: o bloco de
            # status corta detalhes longos pelo fim.
            reason = (
                f"{reason} · escrita bloqueada pelo sandbox do workspace "
                f"(/sandbox status)"
            )
        summary = summarize_stderr(stderr)
        if summary and summary not in reason:
            reason = f"{reason} · {summary}"
        return MCPConnectError(
            reason,
            name=name or self._name or "",
            command=self.command_label(),
            stderr=stderr,
        )

    def command_label(self) -> str:
        """Comando formatado para diagnóstico sem expor ambiente."""
        return " ".join(shlex.quote(part) for part in self._command)

    def disconnect(self) -> None:
        if self._process:
            try:
                pid = self._process.pid
                pgid = os.getpgid(pid)
                if pgid == pid and pgid != os.getpgrp():
                    os.killpg(pgid, 15)
                else:
                    self._process.terminate()
            except Exception:
                self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    pid = self._process.pid
                    pgid = os.getpgid(pid)
                    if pgid == pid and pgid != os.getpgrp():
                        os.killpg(pgid, 9)
                    else:
                        self._process.kill()
                except Exception:
                    self._process.kill()
            self._process = None

    @property
    def transport_type(self) -> str:
        return "stdio"


class RemoteMCPTransport(StdioMCPTransport):
    """STDIO proxy para MCP remoto com compatibilidade de credenciais OAuth."""

    def __init__(
        self,
        endpoint: str,
        command: list[str],
        env: dict[str, str] | None = None,
        name: str | None = None,
        workspace=None,
    ) -> None:
        super().__init__(command, env=env, name=name, workspace=workspace)
        self._remote_endpoint = endpoint

    def sandbox_rw_paths(self) -> list[str]:
        # O runner pode ser sobrescrito (QUIMERA_MCP_REMOTE_CMD) sem conter
        # "mcp-remote" no nome; o transporte remote sempre usa o store OAuth.
        return [_ensure_directory(remote_config_dir(self._env))]

    def connect(self) -> tuple[IO[str], IO[str]]:
        migration = migrate_legacy_remote_credentials(
            self._remote_endpoint,
            env=self._env,
        )
        if migration.migrated:
            _logger.info(
                "MCP remote: credenciais OAuth migradas de %s para %s",
                migration.source_store,
                migration.destination_store,
            )
        return super().connect()


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

    def send_mcp_notification(
        self, method: str, params: dict | None = None
    ) -> None:
        """Envia uma notificação MCP HTTP sem ``id`` JSON-RPC."""
        body = _build_request(method, params, msg_id=None)
        self._http_request(
            "POST",
            headers={"Content-Type": "application/json"},
            data=body,
        )

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

    @property
    def transport(self) -> MCPTransport:
        return self._transport

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
                error = self._transport.describe_failure(exc, name=self._name)
                # Comando e stderr completos só no log do app; o chat e o MCP
                # Hub recebem apenas ``str(error)``.
                _logger.error(
                    "MCP client '%s': falha no handshake\n%s",
                    self._name,
                    error.diagnostics,
                )
                raise error from exc
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
            self._transport.send_mcp_notification(method, params)
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
        self._states: dict[str, MCPConnectionState] = {}
        self._listeners: list[Callable[[], None]] = []
        # Geração da tentativa corrente por conexão e o transporte cujo
        # handshake está em andamento; ver ``_begin_attempt``.
        self._attempts: dict[str, int] = {}
        self._inflight: dict[str, tuple[int, MCPTransport]] = {}

    @property
    def started(self) -> bool:
        return self._started

    @property
    def sessions(self) -> dict[str, MCPClientSession]:
        with self._lock:
            return dict(self._sessions)

    # ── Estado observável ────────────────────────────────────────────

    def states(self) -> dict[str, MCPConnectionState]:
        """Snapshot do estado de todas as conexões conhecidas, na ordem de registro."""
        with self._lock:
            return dict(self._states)

    def state(self, name: str) -> MCPConnectionState | None:
        """Estado atual de uma conexão específica, se conhecida."""
        with self._lock:
            return self._states.get(name)

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Registra observador chamado a cada mudança de estado.

        O listener roda na thread que provocou a mudança (normalmente uma
        thread de conexão em background); deve ser rápido e thread-safe.
        Retorna a função de cancelamento da inscrição.
        """
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def _notify_listeners(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                _logger.exception("MCP bridge: listener de estado falhou")

    def _set_state(
        self,
        name: str,
        *,
        phase: str | None = None,
        transport: str | None = None,
        detail: str | None = None,
        tools: int | None = None,
        auth_url: str | None = None,
    ) -> MCPConnectionState:
        with self._lock:
            current = self._states.get(name) or MCPConnectionState(name=name)
            updated = MCPConnectionState(
                name=name,
                transport=current.transport if transport is None else str(transport),
                phase=current.phase if phase is None else str(phase),
                detail=current.detail if detail is None else str(detail),
                tools=current.tools if tools is None else int(tools),
                updated_at=time.time(),
                auth_url=current.auth_url if auth_url is None else str(auth_url),
            )
            self._states[name] = updated
        self._notify_listeners()
        return updated

    def mark_pending(self, name: str, transport: str = "") -> MCPConnectionState:
        """Declara uma conexão configurada cujo handshake ainda não começou."""
        return self._set_state(
            name,
            phase=MCPConnectionPhase.PENDING,
            transport=transport,
            detail="",
            tools=0,
            auth_url="",
        )

    def mark_connecting(self, name: str, transport: str = "") -> MCPConnectionState:
        """Marca o início do handshake de uma conexão."""
        return self._set_state(
            name,
            phase=MCPConnectionPhase.CONNECTING,
            transport=transport,
            detail="",
            tools=0,
            auth_url="",
        )

    def mark_failed(
        self, name: str, error: object, transport: str | None = None
    ) -> MCPConnectionState:
        """Registra falha de conexão com o erro apresentável ao usuário."""
        return self._set_state(
            name,
            phase=MCPConnectionPhase.FAILED,
            transport=transport,
            detail=str(error),
            tools=0,
            auth_url="",
        )

    def set_state_detail(self, name: str, detail: str) -> MCPConnectionState:
        """Atualiza só o aviso operacional da conexão."""
        return self._set_state(name, detail=detail)

    def set_auth_url(
        self, name: str, url: str, *, transport: MCPTransport | None = None
    ) -> MCPConnectionState | None:
        """Publica a URL de autorização OAuth pedida pelo servidor.

        Com ``transport`` informado, o aviso só é aceito se vier do handshake
        em andamento ou da sessão viva da conexão — uma tentativa abandonada
        pode ainda emitir sua URL depois que outra começou.
        """
        if transport is not None and not self._transport_is_active(name, transport):
            _logger.debug(
                "MCP bridge '%s': URL de autorização de transporte substituído ignorada",
                name,
            )
            return None
        return self._set_state(name, auth_url=str(url or ""))

    def _transport_is_active(self, name: str, transport: MCPTransport) -> bool:
        with self._lock:
            inflight = self._inflight.get(name)
            session = self._sessions.get(name)
        if inflight is not None and inflight[1] is transport:
            return True
        return session is not None and getattr(session, "transport", None) is transport

    def forget_connection(self, name: str) -> None:
        """Remove o estado de uma conexão que deixou de estar configurada."""
        with self._lock:
            removed = self._states.pop(name, None) is not None
        if removed:
            self._notify_listeners()

    def _update_tool_counts(self, counts: dict[str, int]) -> None:
        with self._lock:
            changed = False
            for name, count in counts.items():
                current = self._states.get(name)
                if current is None or current.tools == count:
                    continue
                self._states[name] = MCPConnectionState(
                    name=name,
                    transport=current.transport,
                    phase=current.phase,
                    detail=current.detail,
                    tools=int(count),
                    updated_at=time.time(),
                )
                changed = True
        if changed:
            self._notify_listeners()

    # ── Sessões ──────────────────────────────────────────────────────

    @staticmethod
    def _release_failed_transport(name: str, transport: MCPTransport) -> None:
        """Encerra o transporte de um handshake que falhou (processo, sessão HTTP)."""
        try:
            transport.disconnect()
        except Exception as exc:
            _logger.debug(
                "MCP bridge '%s': erro ao encerrar transporte após falha: %s",
                name,
                exc,
            )

    # ── Tentativas de handshake ──────────────────────────────────────

    def _begin_attempt(self, name: str, transport: MCPTransport) -> int:
        """Registra uma nova tentativa de handshake e abandona a anterior.

        Cada tentativa recebe uma geração; só a corrente pode publicar o
        resultado. O transporte da tentativa anterior é encerrado (o processo
        preso em OAuth morre), o que libera a thread que o aguardava com
        :class:`MCPConnectSuperseded`.
        """
        with self._lock:
            attempt = self._attempts.get(name, 0) + 1
            self._attempts[name] = attempt
            previous = self._inflight.pop(name, None)
            self._inflight[name] = (attempt, transport)
        if previous is not None:
            _logger.info(
                "MCP bridge '%s': handshake anterior abandonado por nova tentativa",
                name,
            )
            self._release_failed_transport(name, previous[1])
        return attempt

    def _attempt_is_current(self, name: str, attempt: int) -> bool:
        with self._lock:
            return self._attempts.get(name) == attempt

    def _end_attempt(self, name: str, attempt: int) -> None:
        with self._lock:
            inflight = self._inflight.get(name)
            if inflight is not None and inflight[0] == attempt:
                self._inflight.pop(name)

    def abort_inflight(self, name: str) -> bool:
        """Cancela o handshake em andamento de ``name``, se houver.

        Encerra o transporte (processo/sessão HTTP) e invalida a geração, de
        modo que a thread da tentativa não publique estado. Retorna True se
        havia handshake em andamento.
        """
        with self._lock:
            previous = self._inflight.pop(name, None)
            if previous is not None:
                self._attempts[name] = self._attempts.get(name, 0) + 1
        if previous is None:
            return False
        _logger.info("MCP bridge '%s': handshake em andamento cancelado", name)
        self._release_failed_transport(name, previous[1])
        return True

    def connecting(self, name: str) -> bool:
        """Há um handshake em andamento para ``name``?"""
        with self._lock:
            return name in self._inflight

    # ── Sessões ──────────────────────────────────────────────────────

    def add_connection(
        self, name: str, transport: MCPTransport
    ) -> MCPClientSession:
        """Conecta ``name``; equivale a :meth:`replace_connection` sem sessão prévia."""
        return self.replace_connection(name, transport)

    def replace_connection(
        self, name: str, transport: MCPTransport
    ) -> MCPClientSession:
        """Conecta a nova sessão antes de substituir uma conexão existente.

        A troca é transacional do ponto de vista do bridge: se o novo handshake
        falhar, a sessão antiga continua registrada e utilizável — o estado
        publicado segue ``connected`` com o motivo da reconexão falha em
        ``detail``. Um handshake anterior ainda em andamento para o mesmo nome
        é abandonado (ver :meth:`_begin_attempt`).
        """
        new_session = MCPClientSession(transport, name=name)
        attempt = self._begin_attempt(name, transport)
        with self._lock:
            had_session = name in self._sessions
        if not had_session:
            self.mark_connecting(name, transport.transport_type)
        try:
            new_session.connect()
        except Exception as exc:
            self._end_attempt(name, attempt)
            if not self._attempt_is_current(name, attempt):
                self._release_failed_transport(name, transport)
                raise MCPConnectSuperseded(
                    f"handshake de '{name}' abandonado por nova tentativa", name=name
                ) from exc
            if had_session:
                # A sessão antiga segue viva: continua "connected", com o
                # motivo da reconexão falha visível no detalhe.
                self._set_state(
                    name,
                    phase=MCPConnectionPhase.CONNECTED,
                    detail=f"reconexão falhou: {exc}",
                    auth_url="",
                )
            else:
                self.mark_failed(name, exc, transport=transport.transport_type)
            self._release_failed_transport(name, transport)
            raise
        with self._lock:
            current = self._attempts.get(name) == attempt
            if current:
                self._inflight.pop(name, None)
                old_session = self._sessions.get(name)
                self._sessions[name] = new_session
                self._started = True
        if not current:
            # Outra tentativa (ou um desconectar) venceu enquanto o handshake
            # terminava: a sessão recém-aberta não pode entrar no bridge.
            try:
                new_session.disconnect()
            except Exception:
                _logger.debug("MCP bridge '%s': erro ao descartar sessão superada", name)
            raise MCPConnectSuperseded(
                f"handshake de '{name}' abandonado por nova tentativa", name=name
            )
        self._set_state(
            name,
            phase=MCPConnectionPhase.CONNECTED,
            transport=transport.transport_type,
            detail="",
            tools=0,
            auth_url="",
        )
        if old_session is not None:
            try:
                old_session.disconnect()
            except Exception as exc:
                _logger.warning(
                    "MCP bridge: erro ao encerrar conexão anterior '%s': %s",
                    name,
                    exc,
                )
        _logger.info(
            "MCP bridge: '%s' reconectado (%s)", name, transport.transport_type
        )
        return new_session

    def disconnect_connection(self, name: str, *, forget: bool = False) -> bool:
        """Desconecta uma sessão específica e a remove do bridge.

        Com ``forget=True`` o estado da conexão também é descartado (a
        configuração deixou de existir); caso contrário ela segue visível
        como ``disconnected`` e pode ser reconectada pelo MCP Hub.
        """
        aborted = self.abort_inflight(name)
        with self._lock:
            session = self._sessions.pop(name, None)
            self._started = bool(self._sessions)
        if forget:
            self.forget_connection(name)
        elif session is not None or aborted or self.state(name) is not None:
            self._set_state(
                name,
                phase=MCPConnectionPhase.DISCONNECTED,
                detail="",
                tools=0,
                auth_url="",
            )
        if session is None:
            return aborted
        try:
            session.disconnect()
        finally:
            _logger.info("MCP bridge: '%s' desconectado", name)
        return True

    def register_handlers(self, registry) -> list[str]:
        """Descobre tools de todas as sessões e registra handlers no ToolRegistry.

        Returns:
            Lista de nomes de tools registradas.
        """
        registered = []
        all_schemas: list[dict] = []
        tool_counts: dict[str, int] = {}

        # Snapshot: conexões em background podem entrar no dict durante a
        # iteração; elas serão incluídas no próximo refresh_registration.
        for session_name, session in list(self.sessions.items()):
            try:
                tools = session.list_tools()
            except Exception as exc:
                _logger.warning(
                    "MCP bridge '%s': falha ao listar tools: %s",
                    session_name,
                    exc,
                )
                continue

            tool_counts[session_name] = 0
            effective_prefix = f"{session_name}_"
            for tool in tools:
                tool_name = tool.get("name", "")
                if not tool_name:
                    continue

                local_name = f"{effective_prefix}{tool_name}"
                if local_name in registry.names():
                    _logger.warning(
                        "MCP bridge '%s': tool '%s' ignorada porque o nome local '%s' já existe",
                        session_name,
                        tool_name,
                        local_name,
                    )
                    continue
                description = tool.get("description", "")
                input_schema = tool.get(
                    "inputSchema", {"type": "object", "properties": {}}
                )

                handler = self._make_handler(
                    session, tool_name, description, input_schema
                )
                registry.register(local_name, handler)
                registered.append(local_name)
                tool_counts[session_name] += 1

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
        self._update_tool_counts(tool_counts)

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
                content_blocks = []
                for part in content_parts:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(str(part.get("text", "")))
                        else:
                            content_blocks.append(dict(part))
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
                    content_blocks=content_blocks,
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
        with self._lock:
            inflight = list(self._inflight)
        for name in inflight:
            # Sem isso um ``mcp-remote`` esperando OAuth sobreviveria ao app.
            self.abort_inflight(name)
        for name, session in self.sessions.items():
            try:
                session.disconnect()
                _logger.info("MCP bridge: '%s' desconectado", name)
            except Exception as exc:
                _logger.warning(
                    "MCP bridge '%s': erro ao desconectar: %s", name, exc
                )
        with self._lock:
            self._sessions.clear()
            self._started = False
            self._states = {
                name: MCPConnectionState(
                    name=name,
                    transport=state.transport,
                    phase=MCPConnectionPhase.DISCONNECTED,
                    tools=0,
                    updated_at=time.time(),
                )
                for name, state in self._states.items()
            }
        with self._schema_lock:
            self._schemas.clear()


# ── Factory ──────────────────────────────────────────────────────────────


DEFAULT_MCP_REMOTE_VERSION = "0.3.2"
DEFAULT_MCP_REMOTE_RUNNER = f"npx -y mcp-remote@{DEFAULT_MCP_REMOTE_VERSION}"


def build_mcp_remote_command(endpoint: str) -> list[str]:
    """Expande o atalho ``remote:`` para o comando do ``mcp-remote``.

    ``endpoint`` é a URL do servidor remoto, opcionalmente seguida de argumentos
    extras do ``mcp-remote`` (ex.: ``--header ...``). O runner padrão fixa a
    versão conhecida/testada do pacote e pode ser sobrescrito via
    ``QUIMERA_MCP_REMOTE_CMD`` (útil para testar outra versão ou executor).
    """
    tail = shlex.split(endpoint or "")
    if not tail:
        return []
    runner = os.environ.get("QUIMERA_MCP_REMOTE_CMD", DEFAULT_MCP_REMOTE_RUNNER).strip()
    return shlex.split(runner) + tail


def parse_mcp_client_spec(
    spec: str,
    env_overrides: dict[str, dict[str, str]] | None = None,
    workspace=None,
) -> tuple[str, MCPTransport]:
    """Interpreta uma especificação ``--mcp-client``.

    Formatos aceitos:

    * ``nome=remote:https://host/mcp`` — atalho para servidores OAuth remotos;
      expande para a versão testada do ``mcp-remote``
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
        return name, StdioMCPTransport(
            args,
            env=env_override or None,
            name=name,
            workspace=workspace,
        )
    if transport_type == "remote":
        command = build_mcp_remote_command(endpoint)
        if not command:
            raise ValueError(
                f"Transporte remote exige uma URL em --mcp-client: {spec!r}. "
                f"Ex: nome=remote:https://mcp.exemplo.com/sse"
            )
        return name, RemoteMCPTransport(
            endpoint,
            command,
            env=env_override or None,
            name=name,
            workspace=workspace,
        )
    if transport_type == "socket":
        return name, SocketMCPTransport(endpoint)

    raise ValueError(
        f"Transporte desconhecido em --mcp-client: {transport_type!r}. "
        f"Esperado: stdio, remote, socket, http, https"
    )


def build_bridge_from_cli(
    specs: list[str],
    env_overrides: dict[str, dict[str, str]] | None = None,
    workspace=None,
) -> MCPClientBridge:
    """Constrói e conecta um MCPClientBridge a partir de especificações CLI."""
    bridge = MCPClientBridge()
    for spec in specs:
        display_name = spec
        try:
            name, transport = parse_mcp_client_spec(spec, env_overrides, workspace=workspace)
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


def spec_transport_type(spec: str) -> str:
    """Deduz o transporte declarado em uma spec ``nome=...`` sem abrir conexão."""
    rest = spec.split("=", 1)[1].strip() if "=" in spec else ""
    if rest.startswith("http://") or rest.startswith("https://"):
        return "http"
    if ":" in rest:
        return rest.split(":", 1)[0].strip().lower()
    return ""


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
    workspace=None,
) -> MCPClientRuntime:
    """Prepara os MCP clients externos e publica o bridge global sem conectar.

    Deve rodar antes da criação do ``QuimeraApp``: o ``ToolExecutor`` encontra
    o bridge (ainda sem sessões) durante o bootstrap e as tools externas passam
    a ser registradas dinamicamente conforme cada handshake completa em
    background — ver :func:`connect_mcp_clients_in_background`. Assim uma
    conexão lenta ou quebrada nunca bloqueia a inicialização da interface; o
    bloco de status do boot e o MCP Hub refletem cada conexão como
    ``pending``/``connecting``/``connected``/``failed``.

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

    from quimera.runtime.drivers.tool_schemas import set_bridge_schemas
    from quimera.runtime.tools.mcp_clients import (
        set_bridge as set_mcp_client_bridge,
    )

    env_overrides = parse_mcp_client_env_specs(env_specs)
    bridge = MCPClientBridge()
    for spec in specs:
        name = _spec_name(spec)
        if name:
            bridge.mark_pending(name, spec_transport_type(spec))
    set_mcp_client_bridge(bridge)
    set_bridge_schemas([])

    if cli_specs:
        config.set_mcp_configuration(specs, env_specs)

    return MCPClientRuntime(
        enabled=True,
        bridge=bridge,
        specs=tuple(specs),
        env_overrides=env_overrides,
    )


def connect_mcp_clients_in_background(
    runtime: MCPClientRuntime | None,
    *,
    executor: Any,
    workspace=None,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
) -> list[threading.Thread]:
    """Conecta cada MCP client preparado por :func:`start_mcp_clients` em background.

    Cada conexão ganha uma thread daemon própria, para que um handshake
    travado (ex.: OAuth aguardando o navegador) não atrase as demais. O
    progresso é publicado no bridge; ao conectar, as tools do servidor são
    registradas no ``executor`` via ``refresh_registration`` e aparecem para
    os agentes na próxima chamada ``tools/list``. Falhas ficam no estado da
    conexão com o erro e podem ser refeitas pelo MCP Hub.
    """
    bridge = getattr(runtime, "bridge", None)
    specs = tuple(getattr(runtime, "specs", ()) or ())
    if bridge is None or not specs:
        return []
    env_overrides = getattr(runtime, "env_overrides", None)
    threads: list[threading.Thread] = []
    for spec in specs:
        name = _spec_name(spec) or spec
        thread = thread_factory(
            target=connect_mcp_client_spec,
            args=(bridge, spec),
            kwargs={
                "env_overrides": env_overrides,
                "workspace": workspace,
                "executor": executor,
            },
            name=f"quimera-mcp-client-{name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def bind_bridge_notices(
    bridge: MCPClientBridge, name: str, transport: MCPTransport
) -> None:
    """Encaminha os avisos do transporte ao estado da conexão no bridge.

    Só a autorização OAuth vira estado visível (``auth_url``): é uma ação do
    usuário. As linhas de erro do stderr já foram para o log do app pelo
    transporte; se o handshake falhar, o resumo chega via
    :class:`MCPConnectError`. Com o callback registrado o transporte também
    deixa de escrever no stderr do processo, o que mantém o chat (Textual ou
    pipe) limpo.
    """

    def _on_notice(kind: str, text: str) -> None:
        if kind == "auth":
            bridge.set_auth_url(name, text, transport=transport)

    transport.set_notice_callback(_on_notice)


def connect_mcp_client_spec(
    bridge: MCPClientBridge,
    spec: str,
    *,
    env_overrides: dict[str, dict[str, str]] | None = None,
    workspace=None,
    executor: Any = None,
) -> bool:
    """Conecta uma spec ao bridge e registra suas tools no executor.

    Nunca propaga exceções: o resultado vai para o estado da conexão no
    bridge (``connected`` ou ``failed`` com o motivo). Retorna True quando a
    conexão está utilizável.
    """
    name = _spec_name(spec) or spec
    try:
        name, transport = parse_mcp_client_spec(
            spec, env_overrides, workspace=workspace
        )
    except Exception as exc:
        _logger.error("MCP client '%s': especificação inválida: %s", name, exc)
        bridge.mark_failed(name, exc, transport=spec_transport_type(spec))
        return False

    bind_bridge_notices(bridge, name, transport)
    try:
        bridge.replace_connection(name, transport)
    except MCPConnectSuperseded:
        # Outra tentativa (reconectar/desconectar pelo hub) assumiu; o estado
        # publicado é o dela.
        _logger.info("MCP client '%s': tentativa substituída por outra", name)
        return False
    except Exception as exc:
        # replace_connection já publicou o estado failed/detail.
        _logger.error("Falha ao conectar MCP client '%s': %s", name, exc)
        return False

    if executor is None:
        return True
    from quimera.runtime.tools.mcp_clients import refresh_registration

    try:
        refresh_registration(executor, bridge)
    except Exception as exc:
        _logger.exception("MCP client '%s': falha ao registrar tools", name)
        bridge.mark_failed(name, f"tools não registradas: {exc}")
        return False
    return True
