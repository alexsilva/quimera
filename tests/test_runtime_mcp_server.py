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

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["path"] = path
        captured["token"] = token
        captured["stdin"] = stdin
        captured["stdout"] = stdout

    monkeypatch.setattr("quimera.runtime.mcp_server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/quimera.sock"])

    from quimera.runtime import mcp_server

    mcp_server.main()
    assert captured["path"] == "/tmp/quimera.sock"
    assert captured["token"] is None


def test_main_connect_socket_com_token_cli(monkeypatch):
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["token"] = token

    monkeypatch.setattr("quimera.runtime.mcp_server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/s.sock", "--token", "mytoken"])

    from quimera.runtime import mcp_server
    mcp_server.main()
    assert captured["token"] == "mytoken"


def test_main_connect_socket_token_via_env(monkeypatch):
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["token"] = token

    monkeypatch.setattr("quimera.runtime.mcp_server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/s.sock"])
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "envtoken")

    from quimera.runtime import mcp_server
    mcp_server.main()
    assert captured["token"] == "envtoken"


def test_main_connect_socket_respeita_quimera_mcp_log_level(monkeypatch):
    captured = {}

    def _fake_proxy(path, *, token=None, stdin=None, stdout=None):
        captured["path"] = path

    monkeypatch.setattr("quimera.runtime.mcp_server._proxy_stdio_to_socket", _fake_proxy)
    monkeypatch.setattr(sys, "argv", ["mcp_server", "--connect-socket", "/tmp/s.sock"])
    monkeypatch.setenv("QUIMERA_MCP_LOG_LEVEL", "INFO")

    from quimera.runtime import mcp_server
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
# Testes de plugin com token
# ---------------------------------------------------------------------------

class TestPluginTokenIntegration:
    def test_codex_inclui_token_no_proxy_args(self):
        from quimera.plugins.codex import CodexPlugin
        plugin = CodexPlugin(name="codex", prefix="/codex", style=("blue", "Codex"),
                             cmd=["codex", "exec"])
        object.__setattr__(plugin, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(plugin, "_mcp_token", "mytoken")

        args = plugin.mcp_server_args("/tmp/test.sock")
        args_json = next(v for i, v in enumerate(args) if args[i - 1] == "-c" and "mcp_servers.quimera.args=" in v)
        proxy_cmd = json.loads(args_json.split("=", 1)[1])
        assert "--token" in proxy_cmd
        assert "mytoken" in proxy_cmd

    def test_codex_sem_token_nao_inclui_token_arg(self):
        from quimera.plugins.codex import CodexPlugin
        plugin = CodexPlugin(name="codex", prefix="/codex", style=("blue", "Codex"),
                             cmd=["codex", "exec"])
        object.__setattr__(plugin, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(plugin, "_mcp_token", None)

        args = plugin.mcp_server_args("/tmp/test.sock")
        args_json = next(v for i, v in enumerate(args) if args[i - 1] == "-c" and "mcp_servers.quimera.args=" in v)
        proxy_cmd = json.loads(args_json.split("=", 1)[1])
        assert "--token" not in proxy_cmd

    def test_claude_inclui_token_no_mcp_config(self):
        from quimera.plugins.claude import ClaudePlugin
        plugin = ClaudePlugin(name="claude", prefix="/claude", style=("magenta", "Claude"),
                              cmd=["claude", "-p"])
        object.__setattr__(plugin, "_mcp_token", "claudetoken")

        args = plugin.mcp_server_args("/tmp/test.sock")
        config_json = args[1]  # "--mcp-config", <json>
        config = json.loads(config_json)
        proxy_args = config["mcpServers"]["quimera"]["args"]
        assert "--token" in proxy_args
        assert "claudetoken" in proxy_args

    def test_claude_sem_token_nao_inclui_token_arg(self):
        from quimera.plugins.claude import ClaudePlugin
        plugin = ClaudePlugin(name="claude", prefix="/claude", style=("magenta", "Claude"),
                              cmd=["claude", "-p"])
        object.__setattr__(plugin, "_mcp_token", None)

        args = plugin.mcp_server_args("/tmp/test.sock")
        config = json.loads(args[1])
        proxy_args = config["mcpServers"]["quimera"]["args"]
        assert "--token" not in proxy_args

    def test_opencode_inclui_token_na_config(self):
        from quimera.plugins.opencode import OpenCodePlugin
        plugin = OpenCodePlugin(name="opencode", prefix="/opencode", style=("blue", "OpenCode"),
                                cmd=["opencode", "run"])
        object.__setattr__(plugin, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(plugin, "_mcp_token", "opencodetoken")

        env = plugin.env_for_cli()
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        cmd = config["mcp"]["quimera"]["command"]
        assert "--token" in cmd
        assert "opencodetoken" in cmd

    def test_opencode_sem_token_nao_inclui_token_arg(self):
        from quimera.plugins.opencode import OpenCodePlugin
        plugin = OpenCodePlugin(name="opencode", prefix="/opencode", style=("blue", "OpenCode"),
                                cmd=["opencode", "run"])
        object.__setattr__(plugin, "_mcp_socket_path", "/tmp/test.sock")
        object.__setattr__(plugin, "_mcp_token", None)

        env = plugin.env_for_cli()
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        cmd = config["mcp"]["quimera"]["command"]
        assert "--token" not in cmd

    def test_set_mcp_socket_config_configura_path_e_token(self):
        from quimera.plugins.base import AgentPlugin
        plugin = AgentPlugin(name="test", prefix="/test", style=("white", "Test"))
        plugin.set_mcp_socket_config("/tmp/test.sock", "mytoken")
        assert plugin._mcp_socket_path == "/tmp/test.sock"
        assert plugin._mcp_token == "mytoken"

    def test_set_mcp_socket_config_com_token_vazio_define_none(self):
        from quimera.plugins.base import AgentPlugin
        plugin = AgentPlugin(name="test", prefix="/test", style=("white", "Test"))
        plugin.set_mcp_socket_config("/tmp/test.sock", "  ")
        assert plugin._mcp_token is None

    def test_configure_mcp_socket_usa_set_mcp_socket_config_quando_disponivel(self):
        """configure_mcp_socket usa set_mcp_socket_config quando o plugin tem o método."""
        from unittest.mock import MagicMock, patch
        plugin = MagicMock()
        plugin.set_mcp_socket_config = MagicMock()
        plugin.set_mcp_socket_path = MagicMock()

        from quimera.app.core import QuimeraApp
        app = object.__new__(QuimeraApp)
        app.get_active_agent_plugins = lambda: [plugin]

        app.configure_mcp_socket("/tmp/test.sock", "tok")
        plugin.set_mcp_socket_config.assert_called_once_with("/tmp/test.sock", "tok")
        plugin.set_mcp_socket_path.assert_not_called()

    def test_configure_mcp_socket_fallback_para_set_mcp_socket_path(self):
        """configure_mcp_socket cai para set_mcp_socket_path quando set_mcp_socket_config ausente."""
        from unittest.mock import MagicMock
        plugin = MagicMock(spec=["set_mcp_socket_path"])
        plugin.set_mcp_socket_path = MagicMock()

        from quimera.app.core import QuimeraApp
        app = object.__new__(QuimeraApp)
        app.get_active_agent_plugins = lambda: [plugin]

        app.configure_mcp_socket("/tmp/test.sock", "tok")
        plugin.set_mcp_socket_path.assert_called_once_with("/tmp/test.sock")

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
        from quimera.plugins.base import AgentPlugin
        plugin = AgentPlugin(name="test", prefix="/test", style=("white", "Test"))
        plugin.set_mcp_socket_config("/tmp/test.sock", "mytoken")
        assert plugin._mcp_token == "mytoken"
        plugin.set_mcp_socket_path(None)
        assert plugin._mcp_socket_path is None
        assert plugin._mcp_token is None

    def test_build_token_args_retorna_lista_com_token(self):
        """_build_token_args deve retornar ['--token', token] quando token está definido."""
        from quimera.plugins.base import AgentPlugin
        plugin = AgentPlugin(name="test", prefix="/test", style=("white", "Test"))
        object.__setattr__(plugin, "_mcp_token", "tok123")
        assert plugin._build_token_args() == ["--token", "tok123"]

    def test_build_token_args_retorna_lista_vazia_sem_token(self):
        """_build_token_args deve retornar [] quando token é None."""
        from quimera.plugins.base import AgentPlugin
        plugin = AgentPlugin(name="test", prefix="/test", style=("white", "Test"))
        assert plugin._build_token_args() == []
