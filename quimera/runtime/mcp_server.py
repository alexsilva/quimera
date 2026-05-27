"""MCPServer: servidor MCP sobre stdio que expõe ferramentas do ToolExecutor.

Implementa o protocolo MCP (Model Context Protocol) versão 2024-11-05
via JSON-RPC 2.0 sobre stdio. Sem dependências externas.

Uso como módulo standalone:
    python -m quimera.runtime.mcp_server

Uso programático:
    executor = ToolExecutor(config, approval_handler)
    server = MCPServer(executor)
    server.serve()
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

from quimera.runtime.approval import AutoApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.drivers.tool_schemas import resolve_tool_schemas

_logger = logging.getLogger(__name__)


def _openai_schema_to_mcp(schema: dict) -> dict:
    """Converte schema OpenAI (type/function) para formato MCP (tools/list)."""
    fn = schema.get("function", {})
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _proxy_stdio_to_socket(
    path: str,
    *,
    token: str | None = None,
    stdin: IO | None = None,
    stdout: IO | None = None,
) -> None:
    """Faz bridge entre stdio e um servidor MCP em socket Unix.

    Se *token* for fornecido, envia a linha de autenticação antes de repassar
    stdin/stdout, conforme o protocolo de auth do MCPServer.
    """
    inp = stdin or sys.stdin
    out = stdout or sys.stdout

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    with sock:
        sock_in = sock.makefile("r", encoding="utf-8", errors="replace")
        sock_out = sock.makefile("w", encoding="utf-8")

        normalized_token = (token or "").strip() or None
        if normalized_token:
            auth_line = json.dumps({"quimera_auth_token": normalized_token}) + "\n"
            sock_out.write(auth_line)
            sock_out.flush()

        def _pump_stdin_to_socket() -> None:
            try:
                for raw in inp:
                    sock_out.write(raw)
                    sock_out.flush()
            finally:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        writer_thread = threading.Thread(target=_pump_stdin_to_socket, daemon=True)
        writer_thread.start()

        try:
            for raw in sock_in:
                out.write(raw)
                out.flush()
        finally:
            writer_thread.join(timeout=0.2)


class MCPServer:
    """Servidor MCP sobre stdio que expõe as ferramentas do ToolExecutor.

    Suporta os métodos obrigatórios do protocolo:
      - initialize
      - initialized (notificação, sem resposta)
      - tools/list
      - tools/call
      - ping
    """

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_NAME = "quimera"
    SERVER_VERSION = "0.1.0"

    def __init__(self, tool_executor, *, auth_token: str | None = None) -> None:
        """Inicializa uma instância de MCPServer.

        Args:
            tool_executor: Executor de ferramentas a expor via MCP.
            auth_token: Token de autenticação por sessão. Quando definido, toda
                conexão via socket Unix deve enviar uma linha JSON de autenticação
                antes de qualquer mensagem MCP.
        """
        self._executor = tool_executor
        self._write_lock = threading.Lock()
        normalized = (auth_token or "").strip()
        self._auth_token: str | None = normalized or None

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _write(self, obj: dict, out: IO) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            out.write(line)
            out.flush()

    # ------------------------------------------------------------------
    # Despacho de métodos
    # ------------------------------------------------------------------

    def _handle(self, msg: dict) -> dict | None:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._ok(msg_id, {
                "protocolVersion": self.PROTOCOL_VERSION,
                "serverInfo": {
                    "name": self.SERVER_NAME,
                    "version": self.SERVER_VERSION,
                },
                "capabilities": {
                    "tools": {},
                },
            })

        if method == "initialized":
            # Notificação — sem id, sem resposta.
            return None

        if method == "ping":
            return self._ok(msg_id, {})

        if method == "tools/list":
            return self._handle_tools_list(msg_id)

        if method == "tools/call":
            return self._handle_tools_call(msg_id, params)

        # Método desconhecido: só responde se há id (requests, não notificações).
        if msg_id is not None:
            return self._err(msg_id, -32601, f"Method not found: {method}")
        return None

    def _handle_tools_list(self, msg_id: Any) -> dict:
        schemas = resolve_tool_schemas(self._executor)
        tools = [_openai_schema_to_mcp(s) for s in schemas]
        return self._ok(msg_id, {"tools": tools})

    def _handle_tools_call(self, msg_id: Any, params: dict) -> dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}

        if not tool_name:
            return self._err(msg_id, -32602, "tools/call: 'name' é obrigatório")

        arg_keys = sorted(str(key) for key in arguments.keys()) if isinstance(arguments, dict) else []
        started_at = time.perf_counter()
        _logger.info("MCP tools/call start tool=%s arg_keys=%s", tool_name, arg_keys)

        try:
            call = ToolCall(name=tool_name, arguments=arguments)
            result = self._executor.execute(call)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            _logger.exception("MCP tools/call error tool=%s duration_ms=%d", tool_name, duration_ms)
            return self._err(msg_id, -32603, f"Internal error: {exc}")

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _logger.info(
            "MCP tools/call done tool=%s ok=%s duration_ms=%d",
            tool_name,
            result.ok,
            duration_ms,
        )

        if result.ok:
            text = result.content or ""
        else:
            error_str = str(result.error) if result.error else "Tool execution failed"
            text = error_str

        return self._ok(msg_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not result.ok,
        })

    # ------------------------------------------------------------------
    # Helpers de resposta JSON-RPC 2.0
    # ------------------------------------------------------------------

    def _ok(self, msg_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(self, msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    # ------------------------------------------------------------------
    # Autenticação de socket
    # ------------------------------------------------------------------

    _AUTH_READLINE_TIMEOUT: float = 5.0  # segundos para receber prelude de auth

    def _authenticate_socket_connection(self, inp: IO) -> bool:
        """Valida a linha de autenticação na abertura de uma conexão socket.

        Retorna True imediatamente se auth_token não estiver configurado.
        Caso contrário, lê a primeira linha (com timeout) e valida o token
        sem logar seu valor.
        """
        if not self._auth_token:
            return True
        # Aplica timeout apenas se o stream tiver socket subjacente acessível.
        raw_sock = getattr(inp, "buffer", None)
        raw_sock = getattr(raw_sock, "raw", None)
        underlying = getattr(raw_sock, "_sock", None) if raw_sock else None
        if underlying is None:
            # Fallback: tenta obter via name (socket fileno não aplicável a StringIO em testes)
            underlying = getattr(inp, "_sock", None)
        previous_timeout = None
        if underlying is not None:
            try:
                previous_timeout = underlying.gettimeout()
            except Exception:  # noqa: BLE001
                previous_timeout = None
            try:
                underlying.settimeout(self._AUTH_READLINE_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
        try:
            first_line = inp.readline()
            if not first_line:
                _logger.warning("MCP auth: conexão encerrada antes da autenticação")
                return False
            payload = json.loads(first_line.strip())
            if payload.get("quimera_auth_token") == self._auth_token:
                return True
            _logger.warning("MCP auth: token inválido — conexão recusada")
            return False
        except Exception:  # noqa: BLE001
            _logger.warning("MCP auth: prelude inválido — conexão recusada")
            return False
        finally:
            # O timeout de auth deve valer só para o prelude.
            # Se mantido, conexões MCP long-lived quebram após poucos segundos de ociosidade.
            if underlying is not None:
                try:
                    underlying.settimeout(previous_timeout)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    # Socket Unix
    # ------------------------------------------------------------------

    def _serve_connection(self, conn: socket.socket) -> None:
        """Serve uma conexão Unix socket até EOF."""
        try:
            with conn:
                inp = conn.makefile("r", encoding="utf-8", errors="replace")
                out = conn.makefile("w", encoding="utf-8")
                if not self._authenticate_socket_connection(inp):
                    return
                self.serve(stdin=inp, stdout=out)
        except Exception:
            _logger.debug("Conexão MCP encerrada com erro", exc_info=True)

    def serve_socket(self, path: str) -> None:
        """Escuta num socket Unix e serve cada cliente numa thread daemon.

        Bloqueia até o socket ser fechado ou o processo encerrado.
        Remove o socket anterior no path, se existir.
        """
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(path)
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                _logger.warning("MCP socket: não foi possível definir permissão 0600 em %s: %s", path, exc)
            srv.listen(5)
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                t = threading.Thread(
                    target=self._serve_connection, args=(conn,), daemon=True
                )
                t.start()
        finally:
            srv.close()
            try:
                os.unlink(path)
            except OSError:
                pass

    def start_background(self, path: str) -> None:
        """Inicia serve_socket em thread daemon e retorna imediatamente."""
        t = threading.Thread(target=self.serve_socket, args=(path,), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    def serve(self, stdin: IO | None = None, stdout: IO | None = None) -> None:
        """Processa mensagens MCP até EOF no stdin.

        Args:
            stdin: stream de entrada (padrão: sys.stdin).
            stdout: stream de saída (padrão: sys.stdout).
        """
        inp = stdin or sys.stdin
        out = stdout or sys.stdout

        for raw_line in inp:
            line = raw_line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                _logger.debug("JSON inválido ignorado: %s — %s", line[:80], exc)
                continue

            if not isinstance(msg, dict):
                continue

            try:
                response = self._handle(msg)
            except Exception as exc:
                _logger.exception("Erro inesperado no handler MCP")
                msg_id = msg.get("id")
                response = self._err(msg_id, -32603, f"Internal error: {exc}") if msg_id is not None else None

            if response is not None:
                self._write(response, out)


# ---------------------------------------------------------------------------
# Ponto de entrada standalone
# ---------------------------------------------------------------------------

def _build_standalone_executor():
    """Constrói um ToolExecutor mínimo para uso standalone."""
    workspace = Path(os.environ.get("QUIMERA_WORKSPACE", os.getcwd()))
    config = ToolRuntimeConfig(workspace_root=workspace)
    approval = AutoApprovalHandler()
    return ToolExecutor(config, approval)


def main() -> None:
    """Inicia o MCPServer sobre stdio com executor autônomo ou proxy de socket."""
    parser = argparse.ArgumentParser(prog="python -m quimera.runtime.mcp_server")
    parser.add_argument(
        "--connect-socket",
        dest="connect_socket",
        default=None,
        help="Conecta no socket Unix informado e faz bridge stdio <-> socket.",
    )
    parser.add_argument(
        "--token",
        dest="token",
        default=None,
        help="Token de autenticação para o socket MCP (ou use QUIMERA_MCP_TOKEN).",
    )
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s mcp_server: %(message)s")
    if args.connect_socket:
        token = args.token or os.environ.get("QUIMERA_MCP_TOKEN") or None
        _proxy_stdio_to_socket(args.connect_socket, token=token)
        return

    executor = _build_standalone_executor()
    server = MCPServer(executor)
    server.serve()


if __name__ == "__main__":
    main()
