"""Testes para quimera.runtime.mcp.server.MCPServer."""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.mcp import MCPServer
from quimera.runtime.mcp.server import _openai_schema_to_mcp, _proxy_stdio_to_socket
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.workspace_policy import WorkspacePolicy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_executor(tool_names=None, call_result=None):
    """Cria um ToolExecutor mínimo com registry e execute mockados."""
    executor = MagicMock()
    names = tool_names or ["read_file", "run_shell"]
    executor.registry.names.return_value = names
    executor.config.db_path = None
    executor.policy.blocked_tools = set()
    if call_result is None:
        call_result = ToolResult(ok=True, tool_name="read_file", content="conteudo")
    executor.execute.return_value = call_result
    return executor


def _make_server(executor=None):
    return MCPServer(executor or _make_executor())


def _exchange(server, *msgs):
    """Envia msgs ao server e retorna lista de respostas JSON."""
    lines = "\n".join(json.dumps(m) for m in msgs) + "\n"
    inp = io.StringIO(lines)
    out = io.StringIO()
    server.serve(stdin=inp, stdout=out)
    responses = []
    for line in out.getvalue().splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


# ---------------------------------------------------------------------------
# _openai_schema_to_mcp
# ---------------------------------------------------------------------------

class TestOpenaiSchemaToMcp:
    def test_converte_campos_basicos(self):
        """Verifica que Test converte campos basicos."""
        schema = {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Lê arquivo",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            },
        }
        result = _openai_schema_to_mcp(schema)
        assert result["name"] == "read_file"
        assert result["description"] == "Lê arquivo"
        assert result["inputSchema"]["required"] == ["path"]

    def test_schema_sem_parameters_usa_padrao(self):
        """Verifica que Test schema sem parameters usa padrao."""
        schema = {"type": "function", "function": {"name": "ping", "description": ""}}
        result = _openai_schema_to_mcp(schema)
        assert result["inputSchema"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_retorna_protocolVersion(self):
        """Verifica que Test retorna protocolversion."""
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == MCPServer.PROTOCOL_VERSION

    def test_retorna_serverInfo(self):
        """Verifica que Test retorna serverinfo."""
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        assert resp["result"]["serverInfo"]["name"] == MCPServer.SERVER_NAME

    def test_retorna_capabilities_com_tools(self):
        """Verifica que Test retorna capabilities com tools."""
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
        assert "tools" in resp["result"]["capabilities"]


# ---------------------------------------------------------------------------
# initialized (notificação)
# ---------------------------------------------------------------------------

class TestInitialized:
    def test_nao_produz_resposta(self):
        """Verifica que Test nao produz resposta."""
        server = _make_server()
        responses = _exchange(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert responses == []


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_retorna_resultado_vazio(self):
        """Verifica que Test retorna resultado vazio."""
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 99, "method": "ping"})
        assert resp["result"] == {}
        assert resp["id"] == 99


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

class TestToolsList:
    def test_retorna_lista_de_tools(self):
        """Verifica que Test retorna lista de tools."""
        server = _make_server()
        with patch("quimera.runtime.mcp.server.resolve_tool_schemas") as mock_resolve:
            mock_resolve.return_value = [
                {"type": "function", "function": {"name": "read_file", "description": "desc", "parameters": {}}},
            ]
            [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})

        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "read_file"
        assert "inputSchema" in tools[0]

    def test_tools_vazio_quando_executor_sem_tools(self):
        """Verifica que Test tools vazio quando executor sem tools."""
        server = _make_server()
        with patch("quimera.runtime.mcp.server.resolve_tool_schemas", return_value=[]):
            [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        assert resp["result"]["tools"] == []


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

class TestToolsCall:
    def test_chama_executor_e_retorna_conteudo(self):
        """Verifica que Test chama executor e retorna conteudo."""
        result = ToolResult(ok=True, tool_name="read_file", content="linhas do arquivo")
        executor = _make_executor(call_result=result)
        server = _make_server(executor)

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
        })

        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["type"] == "text"
        assert resp["result"]["content"][0]["text"] == "linhas do arquivo"
        executor.execute.assert_called_once()
        call_arg: ToolCall = executor.execute.call_args[0][0]
        assert call_arg.name == "read_file"
        assert call_arg.arguments == {"path": "foo.py"}

    def test_mcp_socket_reusa_preview_do_executor_para_tool_sem_approval(self, tmp_path):
        """tools/call via MCP deve acionar o mesmo preview operacional do executor."""
        file_path = tmp_path / "foo.py"
        file_path.write_text("print('ok')\n", encoding="utf-8")
        previews = []
        executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
        executor.set_tool_preview_callback(lambda name, args: previews.append((name, args)))
        server = _make_server(executor)

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 101, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
        })

        assert resp["result"]["isError"] is False
        assert previews == [("read_file", {"path": "foo.py"})]

    def test_mcp_socket_preview_inclui_metadata_do_agente_para_tool_sem_approval(self, tmp_path):
        """Preview de tool sem approval deve receber metadata com agente do socket."""
        file_path = tmp_path / "foo.py"
        file_path.write_text("print('ok')\n", encoding="utf-8")
        previews = []
        executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
        executor.set_tool_preview_callback(
            lambda name, args, metadata=None: previews.append((name, args, metadata))
        )
        server = _make_server(executor)

        out = io.StringIO()
        setattr(out, "_mcp_state", {"agent_name": "codex"})
        server.serve(
            stdin=io.StringIO(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 103,
                        "method": "tools/call",
                        "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
                    }
                )
                + "\n"
            ),
            stdout=out,
        )
        [resp] = [json.loads(line) for line in out.getvalue().splitlines()]

        assert resp["result"]["isError"] is False
        assert previews[0][0] == "read_file"
        assert previews[0][1] == {"path": "foo.py"}
        assert previews[0][2]["trusted_context"].agent_name == "codex"

    def test_mcp_socket_preview_com_approval_negado_nao_executa_handler(self, tmp_path):
        """Tool com approval negado: preview emitido, handler não executa."""
        previews = []
        approval_handler = MagicMock()
        approval_handler.approve.return_value = False
        executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval_handler)
        executor.set_tool_preview_callback(lambda name, args: previews.append((name, args)))
        server = _make_server(executor)

        target_file = tmp_path / "x.txt"

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 102, "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "x.txt", "content": "x", "replace_existing": True},
            },
        })

        assert resp["result"]["isError"] is True
        assert len(previews) == 1
        assert previews[0][0] == "write_file"
        assert previews[0][1] == {"path": "x.txt", "content": "x", "replace_existing": True}
        assert approval_handler.approve.call_count == 1
        assert not target_file.exists()

    def test_mcp_socket_com_approval_aprovado_emite_preview_executa_handler(self, tmp_path):
        """Tool com approval aprovado: preview emitido, handler executado."""
        previews = []
        approval_handler = MagicMock()
        approval_handler.approve.return_value = True
        executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval_handler)
        executor.set_tool_preview_callback(lambda name, args: previews.append((name, args)))
        server = _make_server(executor)

        target_file = tmp_path / "y.txt"

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 105, "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "y.txt", "content": "conteudo", "replace_existing": True},
            },
        })

        assert resp["result"]["isError"] is False
        assert len(previews) == 1
        assert previews[0][0] == "write_file"
        assert previews[0][1] == {"path": "y.txt", "content": "conteudo", "replace_existing": True}
        assert approval_handler.approve.call_count == 1
        assert target_file.exists()
        assert target_file.read_text(encoding="utf-8") == "conteudo"

    def test_mcp_socket_preview_para_tool_auto_aprovada_por_policy(self, tmp_path):
        """Tool auto-aprovada por workspace_policy deve usar preview, não card de approval."""
        previews = []
        approval_handler = MagicMock()
        executor = ToolExecutor(
            ToolRuntimeConfig(
                workspace_root=tmp_path,
                workspace_policy=WorkspacePolicy.autonomous(),
            ),
            approval_handler,
        )
        executor.set_tool_preview_callback(
            lambda name, args, metadata=None: previews.append((name, args, metadata))
        )
        server = _make_server(executor)

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 104, "method": "tools/call",
            "params": {
                "name": "run_shell",
                "arguments": {"command": "pwd"},
            },
        })

        assert resp["result"]["isError"] is False
        assert previews[0][0] == "run_shell"
        assert previews[0][1] == {"command": "pwd"}
        assert approval_handler.approve.call_count == 0

    def test_retorna_is_error_quando_tool_falha(self):
        """Verifica que Test retorna is error quando tool falha."""
        result = ToolResult(ok=False, tool_name="run_shell", error="Permissão negada")
        executor = _make_executor(call_result=result)
        server = _make_server(executor)

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "run_shell", "arguments": {"command": "rm -rf /"}},
        })

        assert resp["result"]["isError"] is True
        assert "Permissão negada" in resp["result"]["content"][0]["text"]

    def test_retorna_erro_quando_name_ausente(self):
        """Verifica que Test retorna erro quando name ausente."""
        server = _make_server()
        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {},
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_retorna_erro_interno_quando_execute_levanta_excecao(self):
        """Verifica que Test retorna erro interno quando execute levanta excecao."""
        executor = _make_executor()
        executor.execute.side_effect = RuntimeError("boom")
        server = _make_server(executor)

        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 13, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {}},
        })

        assert "error" in resp
        assert resp["error"]["code"] == -32603
        assert "boom" in resp["error"]["message"]

    def test_emite_logs_de_execucao_da_tool(self, caplog):
        """Verifica que Test emite logs de execucao da tool."""
        result = ToolResult(ok=True, tool_name="read_file", content="ok")
        executor = _make_executor(call_result=result)
        server = _make_server(executor)
        mcp_logger = logging.getLogger("quimera.runtime.mcp.server")
        mcp_logger.addHandler(caplog.handler)

        try:
            with caplog.at_level(logging.DEBUG, logger="quimera.runtime.mcp.server"):
                _exchange(server, {
                    "jsonrpc": "2.0", "id": 14, "method": "tools/call",
                    "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
                })
        finally:
            mcp_logger.removeHandler(caplog.handler)

        messages = [record.message for record in caplog.records if record.name == "quimera.runtime.mcp.server"]
        assert any("MCP tools/call start tool=read_file" in message for message in messages)
        assert any("MCP tools/call done tool=read_file ok=True" in message for message in messages)


# ---------------------------------------------------------------------------
# Método desconhecido
# ---------------------------------------------------------------------------

class TestUnknownMethod:
    def test_retorna_method_not_found_para_request(self):
        """Verifica que Test retorna method not found para request."""
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 20, "method": "foo/bar"})
        assert resp["error"]["code"] == -32601

    def test_silencioso_para_notificacao_desconhecida(self):
        """Verifica que Test silencioso para notificacao desconhecida."""
        server = _make_server()
        # Notificação: sem id
        responses = _exchange(server, {"jsonrpc": "2.0", "method": "foo/bar"})
        assert responses == []


# ---------------------------------------------------------------------------
# Robustez de entrada
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# serve_socket / start_background
# ---------------------------------------------------------------------------

def _unix_socket_exchange(path: str, *msgs) -> list[dict]:
    """Conecta ao socket Unix, envia msgs, lê respostas até conexão fechar."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    payload = "".join(json.dumps(m) + "\n" for m in msgs)
    s.sendall(payload.encode("utf-8"))
    s.shutdown(socket.SHUT_WR)  # sinaliza EOF para o servidor
    chunks = []
    while True:
        data = s.recv(4096)
        if not data:
            break
        chunks.append(data)
    s.close()
    raw = b"".join(chunks).decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


class TestServeSocket:
    def test_serve_socket_processa_ping(self, tmp_path):
        """Verifica que Test serve socket processa ping."""
        sock_path = str(tmp_path / "mcp_test.sock")
        server = _make_server()
        t = threading.Thread(target=server.serve_socket, args=(sock_path,), daemon=True)
        t.start()
        # Aguarda o socket estar disponível
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.02)
        responses = _unix_socket_exchange(
            sock_path, {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert len(responses) == 1
        assert responses[0]["result"] == {}
        assert responses[0]["id"] == 1

    def test_serve_socket_remove_socket_antigo(self, tmp_path):
        """Verifica que Test serve socket remove socket antigo."""
        sock_path = str(tmp_path / "mcp_old.sock")
        # Cria arquivo fantasma no path do socket
        open(sock_path, "w").close()
        server = _make_server()
        t = threading.Thread(target=server.serve_socket, args=(sock_path,), daemon=True)
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(sock_path)
                    s.close()
                    break
                except OSError:
                    pass
            time.sleep(0.02)
        # Não lança exceção, servidor está rodando


class TestPendingCallsProperty:
    def test_has_pending_calls_reflects_internal_queue(self):
        """has_pending_calls expõe estado público thread-safe para o app."""
        server = _make_server()
        assert server.has_pending_calls is False
        with server._pending_lock:
            server._pending_calls.append({"msg_id": 1})
        assert server.has_pending_calls is True

    def test_serve_socket_multiplos_clientes(self, tmp_path):
        """Verifica que Test serve socket multiplos clientes."""
        sock_path = str(tmp_path / "mcp_multi.sock")
        server = _make_server()
        t = threading.Thread(target=server.serve_socket, args=(sock_path,), daemon=True)
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(sock_path)
                    s.close()
                    break
                except OSError:
                    pass
            time.sleep(0.02)

        results = []
        threads = []
        for i in range(3):
            def _client(idx=i):
                r = _unix_socket_exchange(
                    sock_path, {"jsonrpc": "2.0", "id": idx, "method": "ping"}
                )
                results.append(r[0]["id"])
            th = threading.Thread(target=_client)
            threads.append(th)
            th.start()
        for th in threads:
            th.join(timeout=3)

        assert sorted(results) == [0, 1, 2]

    def test_start_background_retorna_imediatamente(self, tmp_path):
        """Verifica que Test start background retorna imediatamente."""
        sock_path = str(tmp_path / "mcp_bg.sock")
        server = _make_server()
        server.start_background(sock_path)
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.02)
        assert os.path.exists(sock_path)
        responses = _unix_socket_exchange(
            sock_path, {"jsonrpc": "2.0", "id": 7, "method": "ping"}
        )
        assert responses[0]["result"] == {}


class TestSocketProxy:
    def test_proxy_stdio_to_socket_encaminha_request_e_resposta(self, tmp_path):
        """Verifica que Test proxy stdio to socket encaminha request e resposta."""
        sock_path = str(tmp_path / "mcp_proxy.sock")
        server = _make_server()
        server.start_background(sock_path)
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.02)

        request_line = json.dumps({"jsonrpc": "2.0", "id": 42, "method": "ping"}) + "\n"
        inp = io.StringIO(request_line)
        out = io.StringIO()
        _proxy_stdio_to_socket(sock_path, stdin=inp, stdout=out)

        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 1
        assert responses[0]["id"] == 42
        assert responses[0]["result"] == {}


class TestInputRobustness:
    def test_linha_json_invalida_retorna_parse_error(self):
        """Verifica que Test linha json invalida retorna parse error."""
        server = _make_server()
        inp = io.StringIO("isto nao e json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)
        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 2
        assert responses[0].get("error", {}).get("code") == -32700
        assert responses[1]["result"] == {}

    def test_linha_em_branco_e_ignorada(self):
        """Verifica que Test linha em branco e ignorada."""
        server = _make_server()
        inp = io.StringIO("\n\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)
        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 1

    def test_sequencia_de_requests(self):
        """Verifica que Test sequencia de requests."""
        server = _make_server()
        msgs = [
            {"jsonrpc": "2.0", "id": i, "method": "ping"}
            for i in range(5)
        ]
        responses = _exchange(server, *msgs)
        assert len(responses) == 5
        ids = {r["id"] for r in responses}
        assert ids == {0, 1, 2, 3, 4}


def test_main_connect_socket_usa_modo_proxy(monkeypatch):
    """Verifica que Test main connect socket usa modo proxy."""
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["path"] = path
        captured["token"] = token
        captured["stdin"] = stdin
        captured["stdout"] = stdout

    monkeypatch.setattr("quimera.runtime.mcp.server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/quimera.sock"])
    monkeypatch.delenv("QUIMERA_MCP_TOKEN", raising=False)

    from quimera.runtime.mcp import server as mcp_server

    mcp_server.main()
    assert captured["path"] == "/tmp/quimera.sock"
    assert captured["token"] is None


def test_main_connect_socket_com_token_cli(monkeypatch):
    """Verifica que Test main connect socket com token cli."""
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["token"] = token

    monkeypatch.setattr("quimera.runtime.mcp.server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/s.sock", "--token", "mytoken"])

    from quimera.runtime.mcp import server as mcp_server
    mcp_server.main()
    assert captured["token"] == "mytoken"


def test_main_connect_socket_token_via_env(monkeypatch):
    """Verifica que Test main connect socket token via env."""
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["token"] = token

    monkeypatch.setattr("quimera.runtime.mcp.server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/s.sock"])
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "envtoken")

    from quimera.runtime.mcp import server as mcp_server
    mcp_server.main()
    assert captured["token"] == "envtoken"


def test_main_connect_socket_respeita_quimera_mcp_log_level(monkeypatch):
    """Verifica que Test main connect socket respeita quimera mcp log level."""
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["path"] = path

    monkeypatch.setattr("quimera.runtime.mcp.server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/s.sock"])
    monkeypatch.setenv("QUIMERA_MCP_LOG_LEVEL", "INFO")

    from quimera.runtime.mcp import server as mcp_server
    with patch.object(mcp_server.logging, "basicConfig") as mock_basic_config:
        mcp_server.main()

    assert captured["path"] == "/tmp/s.sock"
    kwargs = mock_basic_config.call_args.kwargs
    assert kwargs["level"] == logging.INFO


# ---------------------------------------------------------------------------
# Autenticação via socket
# ---------------------------------------------------------------------------

def _wait_for_socket(path: str, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(path)
                s.close()
                return
            except OSError:
                pass
        time.sleep(0.02)


def _unix_socket_exchange_with_prelude(path: str, prelude: str | None, *msgs) -> list[dict]:
    """Conecta ao socket, envia prelude opcional e mensagens MCP."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    payload = ""
    if prelude is not None:
        payload += prelude + "\n"
    payload += "".join(json.dumps(m) + "\n" for m in msgs)
    s.sendall(payload.encode("utf-8"))
    s.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        data = s.recv(4096)
        if not data:
            break
        chunks.append(data)
    s.close()
    raw = b"".join(chunks).decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


class TestSocketAuth:
    def test_sem_token_aceita_conexao_sem_prelude(self, tmp_path):
        """MCPServer(auth_token=None) deve aceitar conexão sem autenticação."""
        sock_path = str(tmp_path / "mcp_noauth.sock")
        server = MCPServer(_make_executor())
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        responses = _unix_socket_exchange(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert len(responses) == 1
        assert responses[0]["result"] == {}

    def test_com_token_aceita_conexao_com_token_correto(self, tmp_path):
        """MCPServer(auth_token='abc') deve aceitar conexão com token correto."""
        sock_path = str(tmp_path / "mcp_auth_ok.sock")
        server = MCPServer(_make_executor(), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        prelude = json.dumps({"quimera_auth_token": "abc"})
        responses = _unix_socket_exchange_with_prelude(
            sock_path, prelude, {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert len(responses) == 1
        assert responses[0]["result"] == {}

    def test_com_token_rejeita_conexao_sem_prelude(self, tmp_path):
        """MCPServer(auth_token='abc') deve fechar conexão sem prelude."""
        sock_path = str(tmp_path / "mcp_auth_nopre.sock")
        server = MCPServer(_make_executor(), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        # Envia direto sem prelude — conexão deve ser encerrada, resposta vazia
        responses = _unix_socket_exchange(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert responses == []

    def test_com_token_rejeita_json_invalido_no_prelude(self, tmp_path):
        """MCPServer deve rejeitar prelude com JSON malformado."""
        sock_path = str(tmp_path / "mcp_auth_badjson.sock")
        server = MCPServer(_make_executor(), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        responses = _unix_socket_exchange_with_prelude(
            sock_path, "isto nao e json", {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert responses == []

    def test_com_token_rejeita_token_errado(self, tmp_path):
        """MCPServer deve rejeitar prelude com token diferente do esperado."""
        sock_path = str(tmp_path / "mcp_auth_wrong.sock")
        server = MCPServer(_make_executor(), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        prelude = json.dumps({"quimera_auth_token": "WRONG"})
        responses = _unix_socket_exchange_with_prelude(
            sock_path, prelude, {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert responses == []

    def test_proxy_envia_prelude_antes_das_mensagens(self, tmp_path):
        """_proxy_stdio_to_socket com token deve enviar auth antes das mensagens MCP."""
        sock_path = str(tmp_path / "mcp_proxy_auth.sock")
        server = MCPServer(_make_executor(), auth_token="secrettoken")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        request_line = json.dumps({"jsonrpc": "2.0", "id": 42, "method": "ping"}) + "\n"
        inp = io.StringIO(request_line)
        out = io.StringIO()
        _proxy_stdio_to_socket(sock_path, token="secrettoken", stdin=inp, stdout=out)

        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 1
        assert responses[0]["id"] == 42
        assert responses[0]["result"] == {}

    def test_conexao_com_tool_desabilitada_remove_tool_do_tools_list(self, tmp_path):
        """Conexão MCP com tool desabilitada não deve anunciar essa tool."""
        sock_path = str(tmp_path / "mcp_disabled_tool_tools_list.sock")
        server = MCPServer(_make_executor(tool_names=["read_file", "ask_user"]), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        schemas = [
            {"type": "function", "function": {"name": "read_file", "description": "desc", "parameters": {}}},
            {"type": "function", "function": {"name": "ask_user", "description": "desc", "parameters": {}}},
        ]
        prelude = json.dumps({"quimera_auth_token": "abc", "quimera_disabled_tools": "ask_user"})
        with patch("quimera.runtime.mcp.server.resolve_tool_schemas", return_value=schemas):
            [resp] = _unix_socket_exchange_with_prelude(
                sock_path,
                prelude,
                {"jsonrpc": "2.0", "id": 77, "method": "tools/list"},
            )

        names = [tool["name"] for tool in resp["result"]["tools"]]
        assert names == ["read_file"]

    def test_proxy_repassa_disabled_tools_da_env(self, tmp_path, monkeypatch):
        """Proxy MCP deve transformar QUIMERA_MCP_DISABLED_TOOLS em policy da conexão."""
        sock_path = str(tmp_path / "mcp_proxy_disabled_tools.sock")
        server = MCPServer(_make_executor(tool_names=["read_file", "ask_user"]), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        schemas = [
            {"type": "function", "function": {"name": "read_file", "description": "desc", "parameters": {}}},
            {"type": "function", "function": {"name": "ask_user", "description": "desc", "parameters": {}}},
        ]
        request_line = json.dumps({"jsonrpc": "2.0", "id": 79, "method": "tools/list"}) + "\n"
        inp = io.StringIO(request_line)
        out = io.StringIO()
        monkeypatch.setenv("QUIMERA_MCP_DISABLED_TOOLS", "ask_user")

        with patch("quimera.runtime.mcp.server.resolve_tool_schemas", return_value=schemas):
            _proxy_stdio_to_socket(sock_path, token="abc", stdin=inp, stdout=out)

        [resp] = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        names = [tool["name"] for tool in resp["result"]["tools"]]
        assert names == ["read_file"]

    def test_conexao_com_tool_desabilitada_rejeita_tools_call(self, tmp_path):
        """Conexão MCP com tool desabilitada deve bloquear antes do executor."""
        sock_path = str(tmp_path / "mcp_disabled_tool_call.sock")
        executor = _make_executor(tool_names=["ask_user"])
        server = MCPServer(executor, auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        prelude = json.dumps({"quimera_auth_token": "abc", "quimera_disabled_tools": "ask_user"})
        [resp] = _unix_socket_exchange_with_prelude(
            sock_path,
            prelude,
            {
                "jsonrpc": "2.0",
                "id": 78,
                "method": "tools/call",
                "params": {"name": "ask_user", "arguments": {"question": "Continuar?"}},
            },
        )

        assert resp["error"]["code"] == -32602
        assert "Tool disabled" in resp["error"]["message"]
        executor.execute.assert_not_called()

    def test_timeout_de_auth_nao_deve_fechar_conexao_apos_autenticar(self, tmp_path):
        """Timeout usado no prelude não deve permanecer ativo no stream MCP."""
        sock_path = str(tmp_path / "mcp_auth_timeout_scope.sock")
        server = MCPServer(_make_executor(), auth_token="abc")
        server._AUTH_READLINE_TIMEOUT = 0.05
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        try:
            stream = s.makefile("rw", encoding="utf-8")
            stream.write(json.dumps({"quimera_auth_token": "abc"}) + "\n")
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
            stream.flush()

            first = stream.readline()
            assert first
            assert json.loads(first)["result"] == {}

            # Aguarda mais que o timeout de auth; conexão deve seguir viva.
            time.sleep(0.15)
            stream.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
            stream.flush()
            second = stream.readline()
            assert second
            assert json.loads(second)["result"] == {}
        finally:
            s.close()

    def test_socket_criado_com_permissao_0600(self, tmp_path):
        """O socket Unix deve ter permissão 0o600 após o bind."""
        sock_path = str(tmp_path / "mcp_perms.sock")
        server = MCPServer(_make_executor())
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        mode = os.stat(sock_path).st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Testes de profile com token
# ---------------------------------------------------------------------------

class TestProfileTokenIntegration:
    def test_codex_inclui_token_no_proxy_args(self):
        """Verifica que Test codex inclui token no proxy args."""
        from quimera.profiles.codex import CodexProfile
        profile = CodexProfile(name="codex", prefix="/codex", style=("blue", "Codex"),
                             cmd=["codex", "exec"])
        object.__setattr__(profile, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(profile, "_mcp_token", "mytoken")

        args = profile.mcp_server_args("/tmp/test.sock")
        args_json = next(v for i, v in enumerate(args) if args[i - 1] == "-c" and "mcp_servers.quimera.args=" in v)
        proxy_cmd = json.loads(args_json.split("=", 1)[1])
        assert "--token" in proxy_cmd
        assert "mytoken" in proxy_cmd

    def test_codex_sem_token_nao_inclui_token_arg(self):
        """Verifica que Test codex sem token nao inclui token arg."""
        from quimera.profiles.codex import CodexProfile
        profile = CodexProfile(name="codex", prefix="/codex", style=("blue", "Codex"),
                             cmd=["codex", "exec"])
        object.__setattr__(profile, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(profile, "_mcp_token", None)

        args = profile.mcp_server_args("/tmp/test.sock")
        args_json = next(v for i, v in enumerate(args) if args[i - 1] == "-c" and "mcp_servers.quimera.args=" in v)
        proxy_cmd = json.loads(args_json.split("=", 1)[1])
        assert "--token" not in proxy_cmd

    def test_claude_inclui_token_no_mcp_config(self):
        """Verifica que Test claude inclui token no mcp config."""
        from quimera.profiles.claude import ClaudeProfile
        profile = ClaudeProfile(name="claude", prefix="/claude", style=("magenta", "Claude"),
                              cmd=["claude", "-p"])
        object.__setattr__(profile, "_mcp_token", "claudetoken")

        args = profile.mcp_server_args("/tmp/test.sock")
        config_json = args[1]  # "--mcp-config", <json>
        config = json.loads(config_json)
        proxy_args = config["mcpServers"]["quimera"]["args"]
        assert "--token" in proxy_args
        assert "claudetoken" in proxy_args

    def test_claude_sem_token_nao_inclui_token_arg(self):
        """Verifica que Test claude sem token nao inclui token arg."""
        from quimera.profiles.claude import ClaudeProfile
        profile = ClaudeProfile(name="claude", prefix="/claude", style=("magenta", "Claude"),
                              cmd=["claude", "-p"])
        object.__setattr__(profile, "_mcp_token", None)

        args = profile.mcp_server_args("/tmp/test.sock")
        config = json.loads(args[1])
        proxy_args = config["mcpServers"]["quimera"]["args"]
        assert "--token" not in proxy_args

    def test_opencode_inclui_token_na_config(self):
        """Verifica que Test opencode inclui token na config."""
        from quimera.profiles.opencode import OpenCodeProfile
        profile = OpenCodeProfile(name="opencode", prefix="/opencode", style=("blue", "OpenCode"),
                                cmd=["opencode", "run"])
        object.__setattr__(profile, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(profile, "_mcp_token", "opencodetoken")

        env = profile.env_for_cli()
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        cmd = config["mcp"]["quimera"]["command"]
        assert "--token" in cmd
        assert "opencodetoken" in cmd

    def test_opencode_sem_token_nao_inclui_token_arg(self):
        """Verifica que Test opencode sem token nao inclui token arg."""
        from quimera.profiles.opencode import OpenCodeProfile
        profile = OpenCodeProfile(name="opencode", prefix="/opencode", style=("blue", "OpenCode"),
                                cmd=["opencode", "run"])
        object.__setattr__(profile, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(profile, "_mcp_token", None)

        env = profile.env_for_cli()
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        cmd = config["mcp"]["quimera"]["command"]
        assert "--token" not in cmd

    def test_set_mcp_socket_config_configura_path_e_token(self):
        """Verifica que Test set mcp socket config configura path e token."""
        from quimera.profiles.base import ExecutionProfile
        profile = ExecutionProfile(name="test", prefix="/test", style=("white", "Test"))
        profile.set_mcp_socket_config("/tmp/test.sock", "mytoken")
        assert profile._mcp_socket_path == "/tmp/test.sock"
        assert profile._mcp_token == "mytoken"

    def test_set_mcp_socket_config_com_token_vazio_define_none(self):
        """Verifica que Test set mcp socket config com token vazio define none."""
        from quimera.profiles.base import ExecutionProfile
        profile = ExecutionProfile(name="test", prefix="/test", style=("white", "Test"))
        profile.set_mcp_socket_config("/tmp/test.sock", "  ")
        assert profile._mcp_token is None

    def test_configure_mcp_socket_usa_set_mcp_socket_config_quando_disponivel(self):
        """configure_mcp_socket usa set_mcp_socket_config quando o profile tem o método."""
        from unittest.mock import MagicMock, patch
        profile = MagicMock()
        profile.set_mcp_socket_config = MagicMock()
        profile.set_mcp_socket_path = MagicMock()

        from quimera.app.core import QuimeraApp
        app = object.__new__(QuimeraApp)
        app.get_active_agent_profiles = lambda: [profile]

        app.configure_mcp_socket("/tmp/test.sock", "tok")
        profile.set_mcp_socket_config.assert_called_once_with("/tmp/test.sock", "tok")
        profile.set_mcp_socket_path.assert_not_called()

    def test_configure_mcp_socket_fallback_para_set_mcp_socket_path(self):
        """configure_mcp_socket cai para set_mcp_socket_path quando set_mcp_socket_config ausente."""
        from unittest.mock import MagicMock
        profile = MagicMock(spec=["set_mcp_socket_path"])
        profile.set_mcp_socket_path = MagicMock()

        from quimera.app.core import QuimeraApp
        app = object.__new__(QuimeraApp)
        app.get_active_agent_profiles = lambda: [profile]

        app.configure_mcp_socket("/tmp/test.sock", "tok")
        profile.set_mcp_socket_path.assert_called_once_with("/tmp/test.sock")

    def test_com_token_aceita_conexao_com_token_correto_e_responde_a_initialize(self, tmp_path):
        """MCPServer(auth_token='abc') deve aceitar token correto e responder a initialize."""
        sock_path = str(tmp_path / "mcp_auth_initialize.sock")
        server = MCPServer(_make_executor(), auth_token="abc")
        server.start_background(sock_path)
        _wait_for_socket(sock_path)

        prelude = json.dumps({"quimera_auth_token": "abc"})
        responses = _unix_socket_exchange_with_prelude(
            sock_path,
            prelude,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert len(responses) == 1
        assert responses[0]["id"] == 1
        assert responses[0]["result"]["protocolVersion"] == MCPServer.PROTOCOL_VERSION

    def test_set_mcp_socket_path_none_limpa_token(self):
        """set_mcp_socket_path(None) deve limpar _mcp_token para evitar vazamento de estado."""
        from quimera.profiles.base import ExecutionProfile
        profile = ExecutionProfile(name="test", prefix="/test", style=("white", "Test"))
        profile.set_mcp_socket_config("/tmp/test.sock", "mytoken")
        assert profile._mcp_token == "mytoken"
        profile.set_mcp_socket_path(None)
        assert profile._mcp_socket_path is None
        assert profile._mcp_token is None

    def test_build_token_args_retorna_lista_com_token(self):
        """_build_token_args deve retornar ['--token', token] quando token está definido."""
        from quimera.profiles.base import ExecutionProfile
        profile = ExecutionProfile(name="test", prefix="/test", style=("white", "Test"))
        object.__setattr__(profile, "_mcp_token", "tok123")
        assert profile._build_token_args() == ["--token", "tok123"]

    def test_build_token_args_retorna_lista_vazia_sem_token(self):
        """_build_token_args deve retornar [] quando token é None."""
        from quimera.profiles.base import ExecutionProfile
        profile = ExecutionProfile(name="test", prefix="/test", style=("white", "Test"))
        assert profile._build_token_args() == []

class TestLatestMCPFeatures:
    def test_initialize_anuncia_spec_latest_e_capacidades_completas(self):
        """Verifica que Test initialize anuncia spec latest e capacidades completas."""
        server = _make_server()
        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 50, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test"}},
        })
        result = resp["result"]
        assert result["protocolVersion"] == MCPServer.PROTOCOL_VERSION
        assert "resources" in result["capabilities"]
        assert "prompts" in result["capabilities"]
        assert "completions" in result["capabilities"]
        assert result["capabilities"]["tools"]["listChanged"] is True

    def test_resources_list_read_templates_e_subscribe(self, tmp_path):
        """Verifica que Test resources list read templates e subscribe."""
        (tmp_path / "README.md").write_text("# hello", encoding="utf-8")
        executor = _make_executor()
        executor.config.workspace_root = tmp_path
        server = _make_server(executor)

        [listed] = _exchange(server, {"jsonrpc": "2.0", "id": 51, "method": "resources/list"})
        uris = [r["uri"] for r in listed["result"]["resources"]]
        assert "quimera://workspace" in uris
        assert (tmp_path / "README.md").as_uri() in uris

        [read] = _exchange(server, {
            "jsonrpc": "2.0", "id": 52, "method": "resources/read",
            "params": {"uri": (tmp_path / "README.md").as_uri()},
        })
        assert read["result"]["contents"][0]["text"] == "# hello"

        [templates] = _exchange(server, {"jsonrpc": "2.0", "id": 53, "method": "resources/templates/list"})
        assert templates["result"]["resourceTemplates"][0]["uriTemplate"] == "file:///{path}"

        [sub] = _exchange(server, {"jsonrpc": "2.0", "id": 54, "method": "resources/subscribe", "params": {"uri": "quimera://workspace"}})
        assert sub["result"] == {}

    def test_prompts_list_get_e_completion(self):
        """Verifica que Test prompts list get e completion."""
        server = _make_server()
        [listed] = _exchange(server, {"jsonrpc": "2.0", "id": 55, "method": "prompts/list"})
        names = [p["name"] for p in listed["result"]["prompts"]]
        assert "quimera-task" in names

        [prompt] = _exchange(server, {
            "jsonrpc": "2.0", "id": 56, "method": "prompts/get",
            "params": {"name": "quimera-task", "arguments": {"task": "implemente x"}},
        })
        assert prompt["result"]["messages"][0]["role"] == "user"
        assert "implemente x" in prompt["result"]["messages"][0]["content"]["text"]

        [completion] = _exchange(server, {
            "jsonrpc": "2.0", "id": 57, "method": "completion/complete",
            "params": {"ref": {"type": "ref/prompt", "name": "quimera-task"}, "argument": {"name": "name", "value": "task"}},
        })
        assert "quimera-task" in completion["result"]["completion"]["values"]

class TestProfileHTTPIntegration:
    def test_http_config_is_legacy_and_not_injected_without_socket(self):
        """Verifica que Test http config is legacy and not injected without socket."""
        from quimera.profiles.claude import ClaudeProfile
        from quimera.profiles.codex import CodexProfile
        from quimera.profiles.opencode import OpenCodeProfile

        claude = ClaudeProfile(name="c", prefix="/c", style=("x", "C"), cmd=["claude", "-"])
        claude.set_mcp_http_config("http://127.0.0.1:9090/mcp", "tok")
        assert claude.effective_cmd() == ["claude", "-"]
        assert claude.mcp_http_server_args("http://127.0.0.1:9090/mcp") == []

        codex = CodexProfile(name="x", prefix="/x", style=("x", "X"), cmd=["codex", "exec", "-"])
        codex.set_mcp_http_config("http://127.0.0.1:9090/mcp", "tok")
        codex_cmd = codex.effective_cmd()
        assert not any("mcp_servers.quimera.url" in str(part) for part in codex_cmd)
        assert not any("mcp_servers.quimera.transport" in str(part) for part in codex_cmd)

        opencode = OpenCodeProfile(name="o", prefix="/o", style=("x", "O"))
        opencode.set_mcp_http_config("http://127.0.0.1:9090/mcp", "tok")
        assert opencode.env_for_cli() == {}
