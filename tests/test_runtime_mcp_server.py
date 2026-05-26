"""Testes para quimera.runtime.mcp_server.MCPServer."""
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

from quimera.runtime.mcp_server import MCPServer, _openai_schema_to_mcp, _proxy_stdio_to_socket
from quimera.runtime.models import ToolCall, ToolResult


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
        schema = {"type": "function", "function": {"name": "ping", "description": ""}}
        result = _openai_schema_to_mcp(schema)
        assert result["inputSchema"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_retorna_protocolVersion(self):
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == MCPServer.PROTOCOL_VERSION

    def test_retorna_serverInfo(self):
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        assert resp["result"]["serverInfo"]["name"] == MCPServer.SERVER_NAME

    def test_retorna_capabilities_com_tools(self):
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
        assert "tools" in resp["result"]["capabilities"]


# ---------------------------------------------------------------------------
# initialized (notificação)
# ---------------------------------------------------------------------------

class TestInitialized:
    def test_nao_produz_resposta(self):
        server = _make_server()
        responses = _exchange(server, {"jsonrpc": "2.0", "method": "initialized"})
        assert responses == []


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_retorna_resultado_vazio(self):
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 99, "method": "ping"})
        assert resp["result"] == {}
        assert resp["id"] == 99


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

class TestToolsList:
    def test_retorna_lista_de_tools(self):
        server = _make_server()
        with patch("quimera.runtime.mcp_server.resolve_tool_schemas") as mock_resolve:
            mock_resolve.return_value = [
                {"type": "function", "function": {"name": "read_file", "description": "desc", "parameters": {}}},
            ]
            [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})

        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "read_file"
        assert "inputSchema" in tools[0]

    def test_tools_vazio_quando_executor_sem_tools(self):
        server = _make_server()
        with patch("quimera.runtime.mcp_server.resolve_tool_schemas", return_value=[]):
            [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        assert resp["result"]["tools"] == []


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

class TestToolsCall:
    def test_chama_executor_e_retorna_conteudo(self):
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

    def test_retorna_is_error_quando_tool_falha(self):
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
        server = _make_server()
        [resp] = _exchange(server, {
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {},
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_retorna_erro_interno_quando_execute_levanta_excecao(self):
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
        result = ToolResult(ok=True, tool_name="read_file", content="ok")
        executor = _make_executor(call_result=result)
        server = _make_server(executor)
        mcp_logger = logging.getLogger("quimera.runtime.mcp_server")
        mcp_logger.addHandler(caplog.handler)

        try:
            with caplog.at_level(logging.INFO, logger="quimera.runtime.mcp_server"):
                _exchange(server, {
                    "jsonrpc": "2.0", "id": 14, "method": "tools/call",
                    "params": {"name": "read_file", "arguments": {"path": "foo.py"}},
                })
        finally:
            mcp_logger.removeHandler(caplog.handler)

        messages = [record.message for record in caplog.records if record.name == "quimera.runtime.mcp_server"]
        assert any("MCP tools/call start tool=read_file" in message for message in messages)
        assert any("MCP tools/call done tool=read_file ok=True" in message for message in messages)


# ---------------------------------------------------------------------------
# Método desconhecido
# ---------------------------------------------------------------------------

class TestUnknownMethod:
    def test_retorna_method_not_found_para_request(self):
        server = _make_server()
        [resp] = _exchange(server, {"jsonrpc": "2.0", "id": 20, "method": "foo/bar"})
        assert resp["error"]["code"] == -32601

    def test_silencioso_para_notificacao_desconhecida(self):
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

    def test_serve_socket_multiplos_clientes(self, tmp_path):
        sock_path = str(tmp_path / "mcp_multi.sock")
        server = _make_server()
        t = threading.Thread(target=server.serve_socket, args=(sock_path,), daemon=True)
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                break
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
    def test_linha_json_invalida_e_ignorada(self):
        server = _make_server()
        inp = io.StringIO("isto nao e json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)
        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 1
        assert responses[0]["result"] == {}

    def test_linha_em_branco_e_ignorada(self):
        server = _make_server()
        inp = io.StringIO("\n\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        out = io.StringIO()
        server.serve(stdin=inp, stdout=out)
        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(responses) == 1

    def test_sequencia_de_requests(self):
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
    captured = {}

    def _fake_proxy(path, *, stdin=None, stdout=None):
        captured["path"] = path
        captured["stdin"] = stdin
        captured["stdout"] = stdout

    monkeypatch.setattr("quimera.runtime.mcp_server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/quimera.sock"])

    from quimera.runtime import mcp_server

    mcp_server.main()
    assert captured["path"] == "/tmp/quimera.sock"
