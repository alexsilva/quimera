"""O retorno OAuth encerra a espera humana antes de o MCP estar conectado."""
from __future__ import annotations

import json
import shlex
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from quimera.runtime.mcp.client import (
    MCPClientBridge,
    MCPConnectionPhase,
    StdioMCPTransport,
    _read_response,
    bind_bridge_notices,
    connect_mcp_client_spec,
)
from quimera.runtime.mcp.manager import describe_mcp_client_spec

URL = "https://mcp.example.test/mcp"
SPEC = f"jira=remote:{URL}"

_SERVER = r'''
import json
import sys
import time
from pathlib import Path

root, mode = Path(sys.argv[1]), sys.argv[2]
def wait_for(name):
    deadline = time.monotonic() + 5
    while not (root / name).exists():
        assert time.monotonic() < deadline, name
        time.sleep(.005)
def log(text):
    print('[15] ' + text, file=sys.stderr, flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'initialize':
        log('https://auth.example.test/authorize?state=consent')
        wait_for('approval')
        log('Auth code received, resolving promise')
        log('Completing authorization...')
        wait_for('exchange')
        (root / 'tokens.json').write_text('{"access_token":"fresh"}')
        log('Recursively reconnecting for reason: authentication-needed')
        wait_for('initialize')
        if mode == 'timeout':
            log('Fatal error: _McpError: MCP error -32001: Request timed out')
            sys.exit(1)
        result = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'authorized'}}
    elif request['method'] == 'tools/list':
        result = {'tools': [{'name': 'search', 'inputSchema': {'type': 'object'}}]}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''


@pytest.mark.parametrize("mode", ["success", "timeout"])
def test_browser_return_clears_pending_before_initialize_finishes(tmp_path, monkeypatch, mode):
    script = tmp_path / "proxy.py"
    script.write_text(_SERVER)
    monkeypatch.setenv("QUIMERA_MCP_REMOTE_CMD", shlex.join([
        sys.executable, "-u", str(script), str(tmp_path), mode,
    ]))
    monkeypatch.setenv("MCP_REMOTE_CONFIG_DIR", str(tmp_path / "auth"))
    bridge = MCPClientBridge()
    pending, received, completed = (threading.Event() for _ in range(3))

    def observe():
        state = bridge.state("jira")
        if state.auth_pending:
            pending.set()
        if "retorno OAuth recebido" in state.detail:
            received.set()
        if "autorização OAuth concluída" in state.detail:
            completed.set()

    bridge.subscribe(observe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(connect_mcp_client_spec, bridge, SPEC)
        try:
            assert pending.wait(3)
            assert describe_mcp_client_spec(SPEC, state=bridge.state("jira")).state_label == "autorização pendente"
            (tmp_path / "approval").touch()

            assert received.wait(3)
            assert not bridge.state("jira").auth_pending
            assert bridge.state("jira").phase == MCPConnectionPhase.CONNECTING
            assert not future.done()
            (tmp_path / "exchange").touch()

            assert completed.wait(3)
            state = bridge.state("jira")
            assert state.auth_url == ""
            assert describe_mcp_client_spec(SPEC, state=state).state_label == "conectando…"
            assert state.phase == MCPConnectionPhase.CONNECTING
            assert "jira" not in bridge.sessions
            assert not future.done()
            (tmp_path / "initialize").touch()

            assert future.result(timeout=3) is (mode == "success")
            state = bridge.state("jira")
            assert state.auth_url == ""
            assert json.loads((tmp_path / "tokens.json").read_text())["access_token"] == "fresh"
            if mode == "success":
                assert state.connected
                assert len(bridge.sessions["jira"].tools) == 1
                assert state.detail == ""
            else:
                assert state.failed
                assert state.detail.startswith(
                    "autorização OAuth concluída; mcp-remote encerrou após 60s "
                    "sem resposta do servidor remoto ao initialize"
                )
        finally:
            bridge.shutdown()


def test_browser_return_restarts_response_timeout_without_waiting_ten_minutes(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("quimera.runtime.mcp.client.time.monotonic", lambda: clock[0])

    class SilentReader:
        def readline(self, timeout):
            clock[0] += timeout
            raise TimeoutError

    with pytest.raises(TimeoutError, match="sem resposta do servidor para initialize em 1s"):
        _read_response(
            SilentReader(), "init", timeout=1.0, method="initialize",
            authorization_deadline=lambda: 600.0 if clock[0] < 2.0 else None,
            authorization_response_at=lambda: 2.0 if clock[0] >= 2.0 else None,
        )

    assert clock[0] == 3.0


def test_set_auth_progress_clears_the_link_only_for_the_active_transport():
    bridge = MCPClientBridge()
    current = StdioMCPTransport(["unused"])
    stale = StdioMCPTransport(["unused"])
    bridge._begin_attempt("jira", current)
    bridge.mark_connecting("jira", "remote")
    bridge.set_auth_url("jira", "https://auth.example.test/current", transport=current)
    try:
        assert bridge.set_auth_progress("jira", "ignorado", transport=stale) is None
        assert bridge.state("jira").auth_url == "https://auth.example.test/current"
        assert bridge.state("jira").detail == ""

        state = bridge.set_auth_progress("jira", "retorno OAuth recebido", transport=current)
        assert state is not None and state.auth_url == "" and state.detail == "retorno OAuth recebido"
        assert state.phase == MCPConnectionPhase.CONNECTING
    finally:
        bridge.shutdown()


def test_old_oauth_completion_cannot_clear_a_new_authorization():
    bridge = MCPClientBridge()
    old = StdioMCPTransport(["unused"])
    current = StdioMCPTransport(["unused"])
    bridge._begin_attempt("jira", old)
    bridge.mark_connecting("jira", "remote")
    bind_bridge_notices(bridge, "jira", old)
    old._print_stderr_line("https://auth.example.test/old")
    bridge._begin_attempt("jira", current)
    bind_bridge_notices(bridge, "jira", current)
    current._print_stderr_line("https://auth.example.test/current")

    try:
        old._print_stderr_line("[15] Auth code received, resolving promise")
        old._print_stderr_line("[15] Recursively reconnecting for reason: authentication-needed")

        assert bridge.state("jira").auth_url == "https://auth.example.test/current"
        assert bridge.state("jira").detail == ""
        assert old.authorization_deadline() is None
        assert current.authorization_deadline() is not None
    finally:
        bridge.shutdown()
