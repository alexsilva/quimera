"""Recuperação OAuth com subprocesso, handshake e estado do MCP Hub reais."""
from __future__ import annotations

import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest

from quimera.runtime.mcp.client import (
    MCPClientBridge,
    MCPClientSession,
    MCPConnectSuperseded,
    MCPConnectionPhase,
    RemoteMCPTransport,
    StdioMCPTransport,
    connect_mcp_client_spec,
)
from quimera.runtime.mcp.remote_credentials import (
    migrate_legacy_remote_credentials,
    snapshot_remote_credentials,
)

URL = "https://mcp.example.test/mcp"
AUTH_URL = "https://auth.example.test/authorize?state=new-consent"
REFRESH_REJECTED = """[15] Proactive token refresh failed, falling back to the stored token
[15] Fatal error: InvalidRequestError: Incorrect request parameters
    at async refreshAuthorization (file:///mcp-remote/dist/chunk.js:27386:18)
"""

_SERVER = r'''
import json
import sys
import time
from pathlib import Path

token, attempts, approval = map(Path, sys.argv[1:4])
failure, mode = sys.argv[4:6]
number = int(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(str(number))
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'initialize':
        if number == 1 or mode == 'repeat':
            if mode == 'prompt':
                print('https://auth.example.test/authorize?state=new-consent', file=sys.stderr, flush=True)
            print(failure, file=sys.stderr, flush=True)
            sys.exit(1)
        assert not token.exists(), 'a credencial recusada foi reutilizada'
        print('https://auth.example.test/authorize?state=new-consent', file=sys.stderr, flush=True)
        deadline = time.monotonic() + 5
        while not approval.exists():
            assert time.monotonic() < deadline, 'o Hub não recebeu a URL OAuth'
            time.sleep(.01)
        token.write_text(json.dumps({'access_token': 'fresh', 'token_type': 'bearer'}))
        result = {'serverInfo': {'name': 'authorized-server'}, 'protocolVersion': '2025-11-25'}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'search', 'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': 'authorized'}]}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''


def _token_path(root: Path, namespace: str = URL) -> Path:
    server_hash = hashlib.md5(namespace.encode(), usedforsecurity=False).hexdigest()
    path = root / "mcp-remote-v1" / f"{server_hash}_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _credentials(root: Path, namespace: str = URL) -> Path:
    path = _token_path(root, namespace)
    path.write_text(json.dumps({"access_token": "revoked", "refresh_token": "revoked-refresh"}))
    return path


def _proxy(tmp_path, monkeypatch, failure=REFRESH_REJECTED, mode="recover", *, cached=True):
    root = tmp_path / "auth"
    token = _credentials(root) if cached else _token_path(root)
    attempts = tmp_path / "attempts"
    approval = tmp_path / "approval"
    script = tmp_path / "proxy.py"
    script.write_text(_SERVER)
    command = [sys.executable, "-u", str(script), str(token), str(attempts), str(approval), failure, mode]
    monkeypatch.setenv("QUIMERA_MCP_REMOTE_CMD", shlex.join(command))
    monkeypatch.setenv("MCP_REMOTE_CONFIG_DIR", str(root))
    return root, token, attempts, approval, command


@pytest.mark.parametrize("stdio", [False, True])
@pytest.mark.parametrize("failure", [REFRESH_REJECTED, "[15] Fatal error: InvalidGrantError: Token revoked"])
def test_revoked_credential_restarts_oauth_and_connects_the_hub(tmp_path, monkeypatch, stdio, failure):
    root, token, attempts, approval, command = _proxy(tmp_path, monkeypatch, failure)
    client_info = token.with_name(token.name.replace("tokens.json", "client_info.json"))
    client_info.write_text('{"client_id":"keep-registration"}')
    other_token = _credentials(root, "https://another.example.test/mcp")
    bridge = MCPClientBridge()
    states = []

    def observe():
        state = bridge.state("jira")
        states.append(state)
        if state.auth_url:
            assert state.auth_url == AUTH_URL
            assert not token.exists()
            approval.touch()  # Simula o consentimento depois de o Hub mostrar o link.

    bridge.subscribe(observe)
    if stdio:
        original = StdioMCPTransport._spawn_in_keeper_thread

        def spawn(self, _command, env):
            return original(self, command, env)

        monkeypatch.setattr(StdioMCPTransport, "_spawn_in_keeper_thread", spawn)
        spec = f"jira=stdio:npx -y mcp-remote@0.3.2 {URL}"
    else:
        spec = f"jira=remote:{URL}"
    try:
        assert connect_mcp_client_spec(bridge, spec)
        assert attempts.read_text() == "2"
        assert json.loads(token.read_text())["access_token"] == "fresh"
        assert json.loads(client_info.read_text())["client_id"] == "keep-registration"
        assert json.loads(other_token.read_text())["access_token"] == "revoked"
        assert all(state.phase != MCPConnectionPhase.FAILED for state in states)
        assert any("nova autorização" in state.detail for state in states)
        assert any(state.auth_pending for state in states)
        assert bridge.state("jira").connected
        assert bridge.state("jira").auth_url == ""
        assert bridge.state("jira").detail == ""
        assert bridge.sessions["jira"].call_tool("search", {})["content"][0]["text"] == "authorized"
    finally:
        bridge.shutdown()


@pytest.mark.parametrize(("failure", "mode", "cached"), [
    ("[15] Fatal error: TypeError: fetch failed", "recover", True),
    ("[15] Fatal error: Error: Request timed out", "recover", True),
    ("[15] Fatal error: InvalidRequestError: Malformed initialize", "recover", True),
    ("[15] Fatal error: AccessDeniedError: User denied consent", "recover", True),
    (REFRESH_REJECTED, "prompt", True),
    (REFRESH_REJECTED, "recover", False),
])
def test_unrelated_failure_or_new_consent_does_not_clear_credentials(
    tmp_path, monkeypatch, failure, mode, cached
):
    _root, token, attempts, _approval, _command = _proxy(tmp_path, monkeypatch, failure, mode, cached=cached)
    bridge = MCPClientBridge()
    try:
        assert not connect_mcp_client_spec(bridge, f"jira=remote:{URL}")
        assert attempts.read_text() == "1"
        assert token.exists() == cached
        if cached:
            assert json.loads(token.read_text())["access_token"] == "revoked"
        assert bridge.state("jira").failed
    finally:
        bridge.shutdown()


def test_oauth_recovery_is_bounded_when_rejection_persists(tmp_path, monkeypatch):
    _root, token, attempts, _approval, _command = _proxy(tmp_path, monkeypatch, mode="repeat")
    bridge = MCPClientBridge()

    def replace_credential():
        if "nova autorização" in bridge.state("jira").detail:
            token.write_text('{"access_token":"still-refused","refresh_token":"still-refused"}')

    bridge.subscribe(replace_credential)
    try:
        assert not connect_mcp_client_spec(bridge, f"jira=remote:{URL}")
        assert attempts.read_text() == "2"
        assert json.loads(token.read_text())["access_token"] == "still-refused"
        assert bridge.state("jira").failed
        assert not bridge.connecting("jira")
    finally:
        bridge.shutdown()


def test_invalidated_token_is_not_restored_from_legacy_store(tmp_path):
    token = _credentials(tmp_path)
    env = {"MCP_REMOTE_CONFIG_DIR": str(tmp_path)}
    snapshot = snapshot_remote_credentials([URL], env=env)
    legacy = tmp_path / "mcp-remote-0.2.5"
    legacy.mkdir()
    (legacy / token.name).write_text(token.read_text())
    server_hash = token.name.removesuffix("_tokens.json")
    port = 3335 + int(server_hash[:4], 16) % 45816
    (legacy / f"{server_hash}_client_info.json").write_text(json.dumps({
        "client_id": "legacy-client", "redirect_uris": [f"http://localhost:{port}/oauth/callback"],
    }))

    snapshot.invalidate()

    result = migrate_legacy_remote_credentials(URL, env=env)
    assert result.reason == "reauthorization_required"
    assert not token.exists()
    assert (legacy / token.name).exists()


def test_recovery_preserves_credential_already_renewed_by_another_process(tmp_path):
    token = _credentials(tmp_path)
    snapshot = snapshot_remote_credentials([URL], env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)})
    token.write_text('{"access_token":"renewed-elsewhere"}')

    snapshot.invalidate()

    assert json.loads(token.read_text())["access_token"] == "renewed-elsewhere"


def test_cache_namespace_matches_resource_and_literal_headers(tmp_path):
    namespace = URL + '|https://resource.example.test|{"A":"first","Z":"Bearer ${TOKEN}"}'
    token = _credentials(tmp_path, namespace)
    arguments = [URL, "--transport", "http-only", "--resource", "https://resource.example.test",
                 "--header", "Z: Bearer ${TOKEN}", "--header", "A: first"]

    snapshot = snapshot_remote_credentials(arguments, env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path)})

    assert snapshot.token_path == token
    snapshot.invalidate()
    assert not token.exists()


def test_disconnect_during_recovery_cannot_respawn_the_proxy(tmp_path, monkeypatch):
    _root, _token, attempts, _approval, _command = _proxy(tmp_path, monkeypatch)
    bridge = MCPClientBridge()

    def cancel_reauthorization():
        if "nova autorização" in bridge.state("jira").detail:
            bridge.disconnect_connection("jira")

    bridge.subscribe(cancel_reauthorization)
    try:
        assert not connect_mcp_client_spec(bridge, f"jira=remote:{URL}")
        assert attempts.read_text() == "1"
        assert not bridge.connecting("jira")
        assert not bridge.sessions
        assert bridge.state("jira").phase == MCPConnectionPhase.DISCONNECTED
    finally:
        bridge.shutdown()


def test_disconnected_transport_cannot_start_an_oauth_retry(tmp_path, monkeypatch):
    _root, _token, attempts, _approval, command = _proxy(tmp_path, monkeypatch)
    transport = RemoteMCPTransport(URL, command, name="jira")
    transport.disconnect()
    with pytest.raises(MCPConnectSuperseded):
        MCPClientSession(transport, name="jira").connect()
    assert not attempts.exists()
