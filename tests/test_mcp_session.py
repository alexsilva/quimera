from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quimera.runtime.mcp import EmbeddedMCPRuntime, start_embedded_mcp


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
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    return SimpleNamespace(tmp=SimpleNamespace(root=tmp_root))


def test_start_embedded_mcp_socket_default_centraliza_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "socket-token")
    app = _FakeApp()
    workspace = _workspace(tmp_path)

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(app, workspace)

    assert isinstance(runtime, EmbeddedMCPRuntime)
    assert runtime.enabled is True
    assert runtime.transport == "socket"
    assert runtime.token == "socket-token"
    assert runtime.socket_path is not None
    assert runtime.socket_path.startswith(str(tmp_path / "tmp" / "mcp-"))
    assert runtime.socket_path.endswith(".sock")
    mcp_cls.assert_called_once_with(app.tool_executor, auth_token="socket-token")
    mcp_cls.return_value.start_background.assert_called_once_with(runtime.socket_path)
    assert app.socket_configs == [(runtime.socket_path, "socket-token")]
    assert app.mcp_socket_path == runtime.socket_path
    assert app.mcp_http_url is None
    assert app.prompt_builder.session_state["mcp_enabled"] is True
    assert app.prompt_builder.session_state["mcp_socket_path"] == runtime.socket_path
    assert app.prompt_builder.session_state["mcp_http_url"] == ""


def test_start_embedded_mcp_socket_usa_path_explicito(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "explicit-token")
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(
            app,
            _workspace(tmp_path),
            socket_path="/tmp/custom.sock",
            token_env="MY_TOKEN",
        )

    assert runtime.socket_path == "/tmp/custom.sock"
    mcp_cls.return_value.start_background.assert_called_once_with("/tmp/custom.sock")
    assert app.socket_configs == [("/tmp/custom.sock", "explicit-token")]


def test_start_embedded_mcp_http_centraliza_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "http-token")
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls, patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ) as http_cls:
        runtime = start_embedded_mcp(
            app,
            _workspace(tmp_path),
            transport="http",
            http_host="0.0.0.0",
            http_port=9090,
        )

    assert runtime.enabled is True
    assert runtime.transport == "http"
    assert runtime.http_url == "http://0.0.0.0:9090/mcp"
    mcp_cls.assert_called_once_with(app.tool_executor, auth_token="http-token")
    http_cls.assert_called_once_with(mcp_cls.return_value, host="0.0.0.0", port=9090)
    http_cls.return_value.start_background.assert_called_once_with()
    assert app.http_configs == [("http://0.0.0.0:9090/mcp", "http-token")]
    assert app.mcp_socket_path is None
    assert app.mcp_http_url == "http://0.0.0.0:9090/mcp"
    assert app.prompt_builder.session_state["mcp_enabled"] is True
    assert app.prompt_builder.session_state["mcp_socket_path"] == ""
    assert app.prompt_builder.session_state["mcp_http_url"] == "http://0.0.0.0:9090/mcp"


def test_start_embedded_mcp_desabilitado_nao_cria_servidor(tmp_path):
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(app, _workspace(tmp_path), enabled=False)

    mcp_cls.assert_not_called()
    assert runtime == EmbeddedMCPRuntime(enabled=False)
    assert app.socket_configs == [(None, None)]
    assert app.mcp_socket_path is None
    assert app.mcp_http_url is None
    assert app.prompt_builder.session_state["mcp_enabled"] is False


def test_start_embedded_mcp_rejeita_transporte_invalido(tmp_path):
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        try:
            start_embedded_mcp(app, _workspace(tmp_path), transport="stdio")  # type: ignore[arg-type]
        except ValueError as exc:
            assert "Transporte MCP inválido" in str(exc)
        else:
            raise AssertionError("ValueError esperado")

    mcp_cls.assert_not_called()
