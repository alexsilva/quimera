import json
import time
from types import SimpleNamespace
from unittest.mock import ANY, patch

import pytest

from quimera.runtime.mcp import (
    EmbeddedMCPRuntime,
    load_persisted_authorized_clients,
    start_embedded_mcp,
    start_external_mcp_http,
    stop_external_mcp_http,
)
from quimera.runtime.mcp.settings import MCPHttpSettings
from quimera.runtime.mcp.http_server import DEFAULT_HTTP_READ_ONLY_TOOLS
from quimera.session_paths import SessionPaths
from quimera.workspace import Workspace


class _FakeApp:
    def __init__(self):
        self.tool_executor = object()
        self.prompt_builder = SimpleNamespace(session_state={"session_id": "s"})
        self.socket_configs = []
        self.http_configs = []

    def configure_mcp_socket(self, socket_path, token=None):
        self.socket_configs.append((socket_path, token))

    def configure_mcp_http(self, url, token=None):
        self.http_configs.append((url, token))


def _workspace(tmp_path):
    return Workspace(tmp_path)


def test_load_persisted_authorized_clients_is_read_only(tmp_path):
    """O Hub lê grants válidos sem iniciar provider nem regravar o store."""
    store = tmp_path / "mcp-server.json"
    now = time.time()
    payload = {
        "version": 1,
        "clients": [
            {
                "client_id": "chatgpt-id",
                "client_secret": None,
                "client_name": "ChatGPT",
                "redirect_uris": [],
                "grant_types": ["authorization_code", "refresh_token"],
                "scope": "mcp:agent",
                "created_at": now - 10,
                "dynamic": True,
            },
            {
                "client_id": "expired-id",
                "client_secret": None,
                "client_name": "Expirado",
                "redirect_uris": [],
                "grant_types": ["authorization_code", "refresh_token"],
                "scope": "mcp",
                "created_at": now - 20,
                "dynamic": True,
            },
        ],
        "access_tokens": [
            {
                "token": "access-live",
                "client_id": "chatgpt-id",
                "scope": "mcp:agent",
                "resource": "",
                "expires_at": now + 3600,
                "kind": "access",
            }
        ],
        "refresh_tokens": [
            {
                "token": "refresh-expired",
                "client_id": "expired-id",
                "scope": "mcp",
                "resource": "",
                "expires_at": now - 1,
                "kind": "refresh",
            },
            {
                "token": "refresh-orphan",
                "client_id": "orphan-id",
                "scope": "mcp:read",
                "resource": "",
                "expires_at": now + 7200,
                "kind": "refresh",
            },
        ],
    }
    store.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = store.read_bytes()
    workspace = SimpleNamespace(
        mcp_server_file=store,
        runtime_secret_files=(),
    )

    clients = load_persisted_authorized_clients(workspace)

    assert [(item.client_id, item.client_name, item.scope) for item in clients] == [
        ("chatgpt-id", "ChatGPT", "mcp:agent"),
        ("orphan-id", "orphan-id", "mcp:read"),
    ]
    assert all(item.authorized and not item.connected for item in clients)
    assert store.read_bytes() == before


def test_start_embedded_mcp_socket_default_centraliza_startup(tmp_path, monkeypatch):
    """Verifica que start embedded mcp socket default centraliza startup."""
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "external-token-not-used")
    app = _FakeApp()
    workspace = _workspace(tmp_path)
    session_paths = SessionPaths(workspace)

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(app, workspace, session_paths)

    assert isinstance(runtime, EmbeddedMCPRuntime)
    assert runtime.enabled is True
    assert runtime.transport == "socket"
    assert runtime.token == "internal-token"
    assert runtime.internal_mcp_token == "internal-token"
    assert runtime.external_mcp_server is None
    assert runtime.socket_path is not None
    assert runtime.socket_path.startswith(str(session_paths.root / "mcp-"))
    assert runtime.socket_path.endswith(".sock")
    mcp_cls.assert_called_once_with(app.tool_executor, auth_token="internal-token", agent_run_sink=None)
    mcp_cls.return_value.start_background.assert_called_once_with(runtime.socket_path)
    assert app.socket_configs == [(runtime.socket_path, "internal-token")]
    assert app.http_configs == []
    assert app.mcp_socket_path == runtime.socket_path
    assert app.mcp_http_url is None
    assert app.prompt_builder.session_state["mcp_enabled"] is True
    assert app.prompt_builder.session_state["mcp_socket_path"] == runtime.socket_path
    assert app.prompt_builder.session_state["mcp_http_url"] == ""


def test_start_embedded_mcp_socket_usa_path_explicito(tmp_path):
    """Verifica que start embedded mcp socket usa path explicito."""
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        workspace = _workspace(tmp_path)
        runtime = start_embedded_mcp(
            app,
            workspace,
            SessionPaths(workspace),
            socket_path="/tmp/custom.sock",
        )

    assert runtime.socket_path == "/tmp/custom.sock"
    mcp_cls.return_value.start_background.assert_called_once_with("/tmp/custom.sock")
    assert app.socket_configs == [("/tmp/custom.sock", "internal-token")]


def test_start_embedded_mcp_http_centraliza_startup_sem_substituir_socket(tmp_path):
    """Verifica que start embedded mcp http centraliza startup sem substituir socket."""
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls, patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ) as http_cls:
        workspace = _workspace(tmp_path)
        runtime = start_embedded_mcp(
            app,
            workspace,
            SessionPaths(workspace),
            external_http_enabled=True,
            http_host="0.0.0.0",
            http_port=9090,
        )

    assert runtime.enabled is True
    assert runtime.transport == "socket"
    assert runtime.http_url == "http://0.0.0.0:9090/mcp"
    assert runtime.external_mcp_http_url == "http://0.0.0.0:9090/mcp"
    assert mcp_cls.call_count == 2
    assert [call.kwargs["auth_token"] for call in mcp_cls.call_args_list] == ["internal-token", None]
    http_cls.assert_called_once_with(
        mcp_cls.return_value,
        host="0.0.0.0",
        port=9090,
        allowed_tools=DEFAULT_HTTP_READ_ONLY_TOOLS,
        oauth=ANY,
        client_access=ANY,
    )
    oauth = http_cls.call_args.kwargs["oauth"]
    assert oauth.enabled is True
    http_cls.return_value.start_background.assert_called_once_with()
    assert app.socket_configs == [(runtime.socket_path, "internal-token")]
    assert app.http_configs == []
    assert app.mcp_socket_path == runtime.socket_path
    assert app.mcp_http_url == "http://0.0.0.0:9090/mcp"
    assert app.prompt_builder.session_state["mcp_enabled"] is True
    assert app.prompt_builder.session_state["mcp_socket_path"] == runtime.socket_path
    assert app.prompt_builder.session_state["mcp_http_url"] == "http://0.0.0.0:9090/mcp"


def test_start_embedded_mcp_desabilitado_nao_cria_servidor(tmp_path):
    """Verifica que start embedded mcp desabilitado nao cria servidor."""
    app = _FakeApp()
    workspace = _workspace(tmp_path)

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(app, workspace, SessionPaths(workspace), enabled=False)

    mcp_cls.assert_not_called()
    assert runtime == EmbeddedMCPRuntime(enabled=False)
    assert app.socket_configs == [(None, None)]
    assert app.http_configs == [(None, None)]
    assert app.mcp_socket_path is None
    assert app.mcp_http_url is None
    assert app.prompt_builder.session_state["mcp_enabled"] is False


def test_start_embedded_mcp_rejeita_transporte_invalido(tmp_path):
    """Verifica que start embedded mcp rejeita transporte invalido."""
    app = _FakeApp()
    workspace = _workspace(tmp_path)

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        try:
            start_embedded_mcp(
                app,
                workspace,
                SessionPaths(workspace),
                transport="stdio",  # type: ignore[arg-type]
            )
        except ValueError as exc:
            assert "Transporte MCP inválido" in str(exc)
        else:
            raise AssertionError("ValueError esperado")

    mcp_cls.assert_not_called()


def test_external_http_failure_releases_resources_and_can_be_retried(tmp_path):
    app = _FakeApp()
    workspace = _workspace(tmp_path)
    settings = MCPHttpSettings(enabled=True, port=9096)
    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls, patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ) as http_cls, patch("quimera.runtime.mcp.session.build_oauth_provider"):
        http_cls.return_value.start_background.side_effect = OSError("Address already in use")
        with pytest.raises(OSError, match="Address already in use"):
            start_external_mcp_http(app, workspace, settings)

        http_cls.return_value.shutdown.assert_called_once_with()
        assert app.external_mcp_http_server is None
        assert app.mcp_http_url is None
        assert "Address already in use" in app.external_mcp_http_error
        assert app.prompt_builder.session_state["mcp_external_http_url"] == ""

        http_cls.return_value.start_background.side_effect = None
        server = start_external_mcp_http(app, workspace, settings)
        assert app.mcp_http_url == "http://127.0.0.1:9096/mcp"
        assert app.external_mcp_http_error == ""
        assert app.mcp_http_settings == settings
        assert server is app.external_mcp_http_server
        assert stop_external_mcp_http(app)
        assert not stop_external_mcp_http(app)
        assert app.mcp_http_url is None
        assert app.socket_configs == []
        assert app.http_configs == []
        assert mcp_cls.call_count == 2


def test_boot_http_failure_keeps_internal_socket_and_reports_cause(tmp_path):
    app = _FakeApp()
    workspace = _workspace(tmp_path)
    with patch("quimera.runtime.mcp.session.MCPServer"), patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ) as http_cls, patch("quimera.runtime.mcp.session.build_oauth_provider"):
        http_cls.return_value.start_background.side_effect = OSError("porta ocupada")
        runtime = start_embedded_mcp(app, workspace, SessionPaths(workspace), external_http_enabled=True)
    assert runtime.enabled and runtime.internal_mcp_socket_path
    assert runtime.external_mcp_http_server is None
    assert app.socket_configs == [(runtime.socket_path, runtime.token)]
    assert app.external_mcp_http_error == "porta ocupada"


def test_disabled_http_does_not_load_oauth_and_retains_options_for_hub(tmp_path):
    app = _FakeApp()
    workspace = _workspace(tmp_path)
    options = {"issuer": "https://quimera.example.test", "allow_dynamic_registration": False}
    with patch("quimera.runtime.mcp.session.MCPServer"), patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ), patch("quimera.runtime.mcp.session.build_oauth_provider") as provider:
        start_embedded_mcp(app, workspace, SessionPaths(workspace), oauth_options=options)
        provider.assert_not_called()
        start_external_mcp_http(app, workspace, MCPHttpSettings(enabled=True))
        provider.assert_called_once_with(workspace, **options)
        stop_external_mcp_http(app)


def test_http_cannot_start_while_session_is_closing(tmp_path):
    app = _FakeApp()
    app._mcp_closing = True
    with patch("quimera.runtime.mcp.session.MCP_HTTPServer") as http_cls:
        with pytest.raises(RuntimeError, match="encerrando"):
            start_external_mcp_http(app, _workspace(tmp_path), MCPHttpSettings(enabled=True))
    http_cls.assert_not_called()


def test_external_http_real_start_stop_and_restart_preserve_oauth(tmp_path):
    from http.client import HTTPConnection

    from quimera.runtime.mcp.oauth import OAuthConfig, OAuthProvider
    from tests.helpers import make_mcp_executor

    app = _FakeApp()
    app.tool_executor = make_mcp_executor()
    workspace = _workspace(tmp_path)
    oauth = OAuthProvider(OAuthConfig(enabled=True))
    settings = MCPHttpSettings(enabled=True, port=0, allow_tools="read-local")
    try:
        first = start_external_mcp_http(app, workspace, settings, oauth=oauth)
        assert first.port > 0
        assert app.mcp_http_url == f"http://127.0.0.1:{first.port}/mcp"
        assert first.oauth is oauth
        connection = HTTPConnection("127.0.0.1", first.port, timeout=3)
        try:
            connection.request("GET", "/.well-known/oauth-protected-resource/mcp")
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["resource"].endswith("/mcp")
        finally:
            connection.close()
        stop_external_mcp_http(app)
        assert first.httpd is None
        second = start_external_mcp_http(app, workspace, settings, oauth=oauth)
        assert second is not first and second.oauth is oauth
    finally:
        stop_external_mcp_http(app)


def test_external_http_client_access_follows_the_workspace_overlay(tmp_path):
    """O filtro entregue ao servidor HTTP lê a liberação do workspace a cada consulta."""
    from quimera.runtime.mcp.settings import MCPHttpSettingsStore

    app = _FakeApp()
    workspace = _workspace(tmp_path)
    with patch("quimera.runtime.mcp.session.MCPServer"), patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ) as http_cls, patch("quimera.runtime.mcp.session.build_oauth_provider"):
        start_external_mcp_http(app, workspace, MCPHttpSettings(enabled=True, port=9096))
        gate = http_cls.call_args.kwargs["client_access"]
        stop_external_mcp_http(app)

    assert callable(gate)
    assert not gate("ChatGPT")
    MCPHttpSettingsStore.from_workspace(workspace).set_client_active("ChatGPT", True)
    assert gate("ChatGPT")
    assert not gate("Grok")
    # Workspaces mínimos (fixtures) sem overlay não têm filtro: comportamento anterior.
    from quimera.runtime.mcp.session import workspace_client_access

    assert workspace_client_access(SimpleNamespace(mcp_server_file=tmp_path / "x.json")) is None
