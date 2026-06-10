from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quimera.runtime.mcp import EmbeddedMCPRuntime, start_embedded_mcp
from quimera.runtime.mcp.http_server import DEFAULT_HTTP_READ_ONLY_TOOLS


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
    """Verifica que start embedded mcp socket default centraliza startup."""
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "external-token-not-used")
    app = _FakeApp()
    workspace = _workspace(tmp_path)

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(app, workspace)

    assert isinstance(runtime, EmbeddedMCPRuntime)
    assert runtime.enabled is True
    assert runtime.transport == "socket"
    assert runtime.token == "internal-token"
    assert runtime.internal_mcp_token == "internal-token"
    assert runtime.external_mcp_server is None
    assert runtime.socket_path is not None
    assert runtime.socket_path.startswith(str(tmp_path / "tmp" / "mcp-"))
    assert runtime.socket_path.endswith(".sock")
    mcp_cls.assert_called_once_with(app.tool_executor, auth_token="internal-token")
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
        runtime = start_embedded_mcp(
            app,
            _workspace(tmp_path),
            socket_path="/tmp/custom.sock",
            token_env="MY_TOKEN",
        )

    assert runtime.socket_path == "/tmp/custom.sock"
    mcp_cls.return_value.start_background.assert_called_once_with("/tmp/custom.sock")
    assert app.socket_configs == [("/tmp/custom.sock", "internal-token")]


def test_start_embedded_mcp_http_centraliza_startup_sem_substituir_socket(tmp_path, monkeypatch):
    """Verifica que start embedded mcp http centraliza startup sem substituir socket."""
    monkeypatch.setenv("QUIMERA_MCP_TOKEN", "http-token")
    app = _FakeApp()

    with patch("quimera.runtime.mcp.session.secrets.token_urlsafe", return_value="internal-token"), patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls, patch(
        "quimera.runtime.mcp.session.MCP_HTTPServer"
    ) as http_cls:
        runtime = start_embedded_mcp(
            app,
            _workspace(tmp_path),
            external_http_enabled=True,
            http_host="0.0.0.0",
            http_port=9090,
        )

    assert runtime.enabled is True
    assert runtime.transport == "socket"
    assert runtime.http_url == "http://0.0.0.0:9090/mcp"
    assert runtime.external_mcp_http_url == "http://0.0.0.0:9090/mcp"
    assert mcp_cls.call_count == 2
    assert [call.kwargs["auth_token"] for call in mcp_cls.call_args_list] == ["internal-token", "http-token"]
    http_cls.assert_called_once_with(
        mcp_cls.return_value,
        host="0.0.0.0",
        port=9090,
        allowed_tools=DEFAULT_HTTP_READ_ONLY_TOOLS,
    )
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

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        runtime = start_embedded_mcp(app, _workspace(tmp_path), enabled=False)

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

    with patch("quimera.runtime.mcp.session.MCPServer") as mcp_cls:
        try:
            start_embedded_mcp(app, _workspace(tmp_path), transport="stdio")  # type: ignore[arg-type]
        except ValueError as exc:
            assert "Transporte MCP inválido" in str(exc)
        else:
            raise AssertionError("ValueError esperado")

    mcp_cls.assert_not_called()
