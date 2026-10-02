from __future__ import annotations

import io
import logging
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from quimera.config import ConfigManager
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.drivers.tool_schemas import (
    get_bridge_schemas,
    resolve_tool_schemas,
    set_bridge_schemas,
)
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.mcp.client import (
    DEFAULT_MCP_REMOTE_VERSION,
    HttpMCPTransport,
    MCPClientBridge,
    MCPClientSession,
    MCPConnectError,
    MCPConnectSuperseded,
    MCPConnectionPhase,
    RemoteMCPTransport,
    StdioMCPTransport,
    build_mcp_remote_command,
    connect_mcp_client_spec,
    connect_mcp_clients_in_background,
    merge_specs_by_name,
    parse_mcp_client_spec,
    spec_transport_type,
    start_mcp_clients,
    summarize_stderr,
)
from quimera.runtime.mcp.manager import MCPConnectionManager, describe_mcp_client_spec
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.registry import ToolRegistry
from quimera.sandbox.bwrap import SandboxUnavailableError
from quimera.runtime.tools.mcp_clients import get_bridge, set_bridge
from quimera.workspace import Workspace


def test_stdio_mcp_fails_closed_when_workspace_sandbox_is_unavailable(tmp_path):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.workspace_config_file).set_sandbox_enabled(True)
    transport = StdioMCPTransport(["mcp-server"], workspace=workspace)

    with patch("quimera.sandbox.bwrap._find_bwrap_executable", return_value=None), patch(
        "quimera.runtime.mcp.client.subprocess.Popen"
    ) as popen:
        with pytest.raises(SandboxUnavailableError):
            transport.connect()

    popen.assert_not_called()


def test_http_mcp_session_sends_initialized_notification_after_handshake(monkeypatch):
    transport = HttpMCPTransport("https://mcp.example.test/mcp")
    calls = []

    monkeypatch.setattr(
        transport,
        "http_initialize",
        lambda: {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "remote", "version": "1"},
        },
    )
    monkeypatch.setattr(
        transport,
        "send_mcp_notification",
        lambda method, params=None: calls.append((method, params)),
    )

    session = MCPClientSession(transport, name="remote")
    session.connect()

    assert calls == [("notifications/initialized", None)]


class FakeSession:
    transport_type = "fake"

    def __init__(self) -> None:
        self.calls = []

    def list_tools(self):
        return [
            {
                "name": "search_issue",
                "description": "Search Jira issues.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string"},
                    },
                    "required": ["jql"],
                },
            }
        ]

    def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {
            "content": [{"type": "text", "text": "PC-1 Example issue"}],
            "isError": False,
        }


class MultiFakeSession(FakeSession):
    def list_tools(self):
        tools = super().list_tools()
        tools.append(
            {
                "name": "transition_issue",
                "description": "Transition Jira issue.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "transition_id": {"type": "string"},
                    },
                    "required": ["issue_key", "transition_id"],
                },
            }
        )
        return tools


class ImageFakeSession(FakeSession):
    def call_tool(self, name: str, arguments: dict):
        return {
            "content": [
                {"type": "text", "text": "Screenshot salvo."},
                {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"},
            ],
            "isError": False,
        }

def teardown_function() -> None:
    set_bridge(None)
    set_bridge_schemas([])


def test_mcp_client_bridge_registers_external_tools_in_executor_registry(tmp_path):
    bridge = MCPClientBridge()
    session = FakeSession()
    bridge._sessions["jira"] = session
    bridge._started = True

    set_bridge(bridge)

    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace=Workspace(tmp_path),
            require_approval_for_mutations=False,
        ),
        approval_handler=None,
    )

    assert "jira_search_issue" in executor.registry.names()

    result = executor.execute(
        ToolCall(name="jira_search_issue", arguments={"jql": "key = PC-1"})
    )

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.content == "PC-1 Example issue"
    assert session.calls == [("search_issue", {"jql": "key = PC-1"})]


def test_mcp_client_bridge_preserves_non_text_content_blocks(tmp_path):
    handler = MCPClientBridge._make_handler(
        ImageFakeSession(), "screenshot", "Screenshot", {"type": "object"}
    )

    result = handler(ToolCall(name="remote_screenshot", arguments={}))

    assert result.content == "Screenshot salvo."
    assert result.content_blocks == [
        {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}
    ]


def test_mcp_client_bridge_schemas_are_resolved_when_registered(tmp_path):
    bridge = MCPClientBridge()
    bridge._sessions["jira"] = FakeSession()
    bridge._started = True

    set_bridge(bridge)

    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace=Workspace(tmp_path),
            require_approval_for_mutations=False,
        ),
        approval_handler=None,
    )

    names = [schema["function"]["name"] for schema in resolve_tool_schemas(executor)]

    assert "jira_search_issue" in names
    assert get_bridge_schemas()[0]["function"]["name"] == "jira_search_issue"


def test_parse_atlassian_mcp_remote_stdio_command():
    name, transport = parse_mcp_client_spec(
        "atlassian=stdio:npx -y mcp-remote https://mcp.atlassian.com/v1/sse"
    )

    assert name == "atlassian"
    assert isinstance(transport, StdioMCPTransport)
    assert transport._command == [
        "npx",
        "-y",
        "mcp-remote",
        "https://mcp.atlassian.com/v1/sse",
    ]


def test_parse_remote_shortcut_expands_to_mcp_remote_command():
    name, transport = parse_mcp_client_spec(
        "atlassian=remote:https://mcp.atlassian.com/v1/sse"
    )

    assert name == "atlassian"
    assert isinstance(transport, RemoteMCPTransport)
    assert transport._command == [
        "npx",
        "-y",
        f"mcp-remote@{DEFAULT_MCP_REMOTE_VERSION}",
        "https://mcp.atlassian.com/v1/sse",
    ]
    assert transport._name == "atlassian"


def test_parse_remote_shortcut_preserves_extra_mcp_remote_args():
    _, transport = parse_mcp_client_spec(
        "gh=remote:https://api.githubcopilot.com/mcp/ --transport sse-only"
    )

    assert transport._command == [
        "npx",
        "-y",
        f"mcp-remote@{DEFAULT_MCP_REMOTE_VERSION}",
        "https://api.githubcopilot.com/mcp/",
        "--transport",
        "sse-only",
    ]


def test_parse_remote_shortcut_without_url_is_rejected():
    try:
        parse_mcp_client_spec("bad=remote:")
    except ValueError as exc:
        assert "remote" in str(exc)
    else:
        raise AssertionError("esperado ValueError para remote sem URL")


def test_build_mcp_remote_command_honors_runner_override(monkeypatch):
    monkeypatch.setenv("QUIMERA_MCP_REMOTE_CMD", "bunx mcp-remote@0.1.0")

    command = build_mcp_remote_command("https://mcp.example.test/sse")

    assert command == [
        "bunx",
        "mcp-remote@0.1.0",
        "https://mcp.example.test/sse",
    ]


def test_build_mcp_remote_command_pins_tested_default_version(monkeypatch):
    monkeypatch.delenv("QUIMERA_MCP_REMOTE_CMD", raising=False)

    command = build_mcp_remote_command("https://mcp.example.test/mcp")

    assert command == [
        "npx",
        "-y",
        f"mcp-remote@{DEFAULT_MCP_REMOTE_VERSION}",
        "https://mcp.example.test/mcp",
    ]


def test_parse_http_mcp_client_accepts_simple_bearer_token():
    name, transport = parse_mcp_client_spec(
        "jira=https://rovo.example.test/mcp",
        {"jira": {"MCP_TOKEN": "token-abc"}},
    )

    assert name == "jira"
    assert isinstance(transport, HttpMCPTransport)
    assert transport._token == "token-abc"


def test_merge_mcp_client_specs_adds_new_names_and_replaces_existing_names():
    existing = [
        "jira=stdio:jira-v1",
        "github=stdio:github-v1",
    ]

    merged = merge_specs_by_name(
        existing,
        [
            "github=stdio:github-v2",
            "notion=https://notion.example.test/mcp",
        ],
    )

    assert merged == [
        "jira=stdio:jira-v1",
        "github=stdio:github-v2",
        "notion=https://notion.example.test/mcp",
    ]
    assert existing == [
        "jira=stdio:jira-v1",
        "github=stdio:github-v1",
    ]


def _install_fake_session(monkeypatch, *, failing=(), tools_by_name=None):
    """Substitui MCPClientSession por uma sessão que conecta sem transporte real."""
    created = []
    failing = set(failing)
    tools_by_name = dict(tools_by_name or {})

    class FakeMCPClientSession:
        def __init__(self, transport, name="external"):
            self._transport = transport
            self._name = name
            self.connected = False
            created.append(self)

        @property
        def name(self):
            return self._name

        def connect(self):
            if self._name in failing:
                raise ConnectionError(f"{self._name} indisponível")
            self.connected = True

        def disconnect(self):
            self.connected = False

        def list_tools(self):
            return [
                {
                    "name": tool_name,
                    "description": "",
                    "inputSchema": {"type": "object", "properties": {}},
                }
                for tool_name in tools_by_name.get(self._name, ["ping"])
            ]

        def call_tool(self, name, arguments):
            return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", FakeMCPClientSession)
    return created


def test_start_mcp_clients_prepara_bridge_pendente_sem_conectar(monkeypatch):
    """O boot só publica o bridge com estados pending; nenhum handshake roda."""
    captured = {}

    class FakeConfig:
        mcp_clients = ["jira=stdio:jira-cmd"]
        mcp_client_env = ["jira=JIRA_TOKEN=old-token"]

        def set_mcp_configuration(self, specs, env_specs):
            captured["persisted_specs"] = specs
            captured["persisted_env_specs"] = env_specs

    def forbid_handshake(*args, **kwargs):
        raise AssertionError("handshake não deve rodar durante o boot")

    monkeypatch.setattr(MCPClientBridge, "add_connection", forbid_handshake)
    monkeypatch.setattr(MCPClientBridge, "replace_connection", forbid_handshake)

    runtime = start_mcp_clients(
        cli_specs=["github=remote:https://api.githubcopilot.com/mcp/"],
        cli_env_specs=["github=GITHUB_TOKEN=new-token"],
        config=FakeConfig(),
    )

    expected_specs = [
        "jira=stdio:jira-cmd",
        "github=remote:https://api.githubcopilot.com/mcp/",
    ]
    assert runtime.enabled is True
    assert runtime.specs == tuple(expected_specs)
    assert get_bridge() is runtime.bridge
    assert runtime.bridge.sessions == {}
    assert get_bridge_schemas() == []
    states = runtime.bridge.states()
    assert list(states) == ["jira", "github"]
    assert states["jira"].phase == MCPConnectionPhase.PENDING
    assert states["jira"].transport == "stdio"
    assert states["github"].transport == "remote"
    assert captured["persisted_specs"] == expected_specs
    assert captured["persisted_env_specs"] == [
        "jira=JIRA_TOKEN=old-token",
        "github=GITHUB_TOKEN=new-token",
    ]
    assert runtime.env_overrides == {
        "jira": {"JIRA_TOKEN": "old-token"},
        "github": {"GITHUB_TOKEN": "new-token"},
    }


def test_start_mcp_clients_sem_specs_nao_publica_bridge():
    class FakeConfig:
        mcp_clients = None
        mcp_client_env = None

    runtime = start_mcp_clients(cli_specs=None, cli_env_specs=None, config=FakeConfig())

    assert runtime.enabled is False
    assert runtime.bridge is None
    assert get_bridge() is None


def test_spec_transport_type_deduz_transporte_sem_conectar():
    assert spec_transport_type("wiki=http://localhost:3100/mcp") == "http"
    assert spec_transport_type("gh=remote:https://api.githubcopilot.com/mcp/") == "remote"
    assert spec_transport_type("jira=stdio:jira-cmd") == "stdio"
    assert spec_transport_type("sock=socket:/tmp/x.sock") == "socket"
    assert spec_transport_type("invalido") == ""


def test_connect_mcp_clients_in_background_registra_tools_e_isola_falhas(
    monkeypatch, tmp_path
):
    """Cada conexão roda em thread própria; uma falha não afeta as demais."""
    _install_fake_session(
        monkeypatch,
        failing={"broken"},
        tools_by_name={"jira": ["search_issue"]},
    )

    class FakeConfig:
        mcp_clients = ["jira=stdio:jira-cmd", "broken=stdio:broken-cmd"]
        mcp_client_env = None

    runtime = start_mcp_clients(cli_specs=None, cli_env_specs=None, config=FakeConfig())
    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace=Workspace(tmp_path),
            require_approval_for_mutations=False,
        ),
        approval_handler=None,
    )
    assert "jira_search_issue" not in executor.registry.names()
    transitions = []
    runtime.bridge.subscribe(
        lambda: transitions.append(
            {name: state.phase for name, state in runtime.bridge.states().items()}
        )
    )

    threads = connect_mcp_clients_in_background(runtime, executor=executor)

    assert [thread.name for thread in threads] == [
        "quimera-mcp-client-jira",
        "quimera-mcp-client-broken",
    ]
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)

    states = runtime.bridge.states()
    assert states["jira"].phase == MCPConnectionPhase.CONNECTED
    assert states["jira"].tools == 1
    assert states["broken"].phase == MCPConnectionPhase.FAILED
    assert "broken indisponível" in states["broken"].detail
    assert "jira_search_issue" in executor.registry.names()
    assert [schema["function"]["name"] for schema in get_bridge_schemas()] == [
        "jira_search_issue"
    ]
    assert {"jira": "connecting", "broken": "pending"} in transitions or {
        "jira": "connecting",
        "broken": "connecting",
    } in transitions
    assert transitions[-1] == {"jira": "connected", "broken": "failed"}


def test_connect_mcp_clients_in_background_ignora_runtime_vazio():
    assert connect_mcp_clients_in_background(None, executor=MagicMock()) == []

    class FakeConfig:
        mcp_clients = None
        mcp_client_env = None

    runtime = start_mcp_clients(cli_specs=None, cli_env_specs=None, config=FakeConfig())
    assert connect_mcp_clients_in_background(runtime, executor=MagicMock()) == []


def test_connect_mcp_client_spec_marca_falha_para_spec_invalida():
    bridge = MCPClientBridge()

    assert connect_mcp_client_spec(bridge, "bad=ftp:host") is False

    state = bridge.state("bad")
    assert state.phase == MCPConnectionPhase.FAILED
    assert "ftp" in state.detail
    assert state.transport == "ftp"


def test_connect_mcp_client_spec_encaminha_avisos_do_transporte_ao_estado(monkeypatch):
    """A URL de autorização emitida pelo mcp-remote vira ``auth_url`` da conexão."""
    captured = {}

    class SlowSession:
        def __init__(self, transport, name="external"):
            captured["transport"] = transport
            self._name = name

        def connect(self):
            transport = captured["transport"]
            transport._print_stderr_line(
                "[2026-07-10 14:31:00Z] [atlassian] https://auth.atlassian.com/authorize?x=1"
            )
            state = bridge.state("atlassian")
            captured["auth_url_during_connect"] = state.auth_url
            captured["detail_during_connect"] = state.detail
            raise ConnectionError("cancelado")

        def disconnect(self):
            pass

    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", SlowSession)
    bridge = MCPClientBridge()

    assert (
        connect_mcp_client_spec(
            bridge, "atlassian=remote:https://mcp.atlassian.com/v1/sse"
        )
        is False
    )

    assert captured["auth_url_during_connect"] == "https://auth.atlassian.com/authorize?x=1"
    assert captured["detail_during_connect"] == ""
    state = bridge.state("atlassian")
    assert state.phase == MCPConnectionPhase.FAILED
    assert state.detail == "cancelado"
    # A falha encerra a autorização pendente daquela tentativa.
    assert state.auth_url == ""


def test_bridge_add_connection_falha_publica_estado_failed(monkeypatch):
    bridge = MCPClientBridge()
    transport = MagicMock()
    transport.transport_type = "http"
    observed = []
    bridge.subscribe(lambda: observed.append(bridge.state("wiki").phase))

    def fail_connect(self):
        raise ConnectionError("recusada")

    monkeypatch.setattr(MCPClientSession, "connect", fail_connect)

    with pytest.raises(ConnectionError, match="recusada"):
        bridge.add_connection("wiki", transport)

    state = bridge.state("wiki")
    assert state.failed is True
    assert state.detail == "recusada"
    assert state.transport == "http"
    assert "wiki" not in bridge.sessions
    assert observed == ["connecting", "failed"]
    # O transporte do handshake falho é encerrado (sem processo/sessão órfã).
    transport.disconnect.assert_called_once_with()


def test_bridge_add_connection_publica_connected_e_contagem_de_tools(monkeypatch):
    _install_fake_session(monkeypatch, tools_by_name={"jira": ["a", "b"]})
    bridge = MCPClientBridge()
    transport = MagicMock()
    transport.transport_type = "stdio"

    bridge.add_connection("jira", transport)
    state = bridge.state("jira")
    assert state.phase == MCPConnectionPhase.CONNECTED
    assert state.tools == 0

    registered = bridge.register_handlers(ToolRegistry())

    assert registered == ["jira_a", "jira_b"]
    assert bridge.state("jira").tools == 2


def test_bridge_disconnect_connection_mantem_ou_esquece_estado(monkeypatch):
    _install_fake_session(monkeypatch)
    bridge = MCPClientBridge()
    transport = MagicMock()
    transport.transport_type = "stdio"
    bridge.add_connection("jira", transport)
    bridge.add_connection("github", transport)

    assert bridge.disconnect_connection("jira") is True
    assert bridge.state("jira").phase == MCPConnectionPhase.DISCONNECTED

    assert bridge.disconnect_connection("github", forget=True) is True
    assert "github" not in bridge.states()
    assert bridge.disconnect_connection("inexistente") is False


def test_bridge_subscribe_notifica_e_cancela():
    bridge = MCPClientBridge()
    phases = []
    unsubscribe = bridge.subscribe(lambda: phases.append(bridge.state("jira").phase))

    bridge.mark_pending("jira", "stdio")
    bridge.mark_connecting("jira", "stdio")
    unsubscribe()
    bridge.mark_failed("jira", "erro")

    assert phases == ["pending", "connecting"]
    assert bridge.state("jira").phase == MCPConnectionPhase.FAILED


def test_stdio_transport_encaminha_avisos_ao_callback_sem_stderr(capsys):
    transport = StdioMCPTransport(["mcp-remote"], name="atlassian")
    notices = []
    transport.set_notice_callback(lambda kind, text: notices.append((kind, text)))
    url = "https://auth.atlassian.com/authorize?x=1"

    transport._print_stderr_line(f"[2026-07-10 14:31:00Z] [atlassian] {url}")
    transport._print_stderr_line("[2026-07-10 14:31:01Z] [atlassian] Error: unauthorized")

    assert notices == [("auth", url), ("error", "Error: unauthorized")]
    assert capsys.readouterr().err == ""


def test_manager_list_connections_expoe_fase_e_detalhe(tmp_path):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.mcp_config_file).set_mcp_configuration(
        ["jira=stdio:jira-cmd", "wiki=http://localhost:3100/mcp"], []
    )
    bridge = MCPClientBridge()
    bridge.mark_connecting("jira", "stdio")
    bridge.mark_failed("wiki", "Connection refused", transport="http")
    set_bridge(bridge)

    manager = MCPConnectionManager(executor=MagicMock(), workspace=workspace)
    infos = {info.name: info for info in manager.list_connections()}

    assert infos["jira"].connected is False
    assert infos["jira"].in_progress is True
    assert infos["jira"].state_label == "conectando…"
    assert infos["wiki"].failed is True
    assert infos["wiki"].detail == "Connection refused"
    assert infos["wiki"].state_label == "falha"


def test_describe_mcp_client_spec_separa_transporte_e_endpoint():
    info = describe_mcp_client_spec(
        "github=remote:https://api.githubcopilot.com/mcp/",
        connected=True,
    )

    assert info.name == "github"
    assert info.transport == "remote"
    assert info.endpoint == "https://api.githubcopilot.com/mcp/"
    assert info.connected is True


def test_manager_disconnect_remove_tools_vivas_e_preserva_config(monkeypatch, tmp_path):
    captured = {}
    workspace = Workspace(tmp_path)
    config = ConfigManager(workspace.mcp_config_file)
    config.set_mcp_configuration(["jira=stdio:jira-cmd"], [])

    class FakeSession:
        def disconnect(self):
            captured["disconnected"] = True

        def list_tools(self):
            return []

    bridge = MCPClientBridge()
    bridge._sessions["jira"] = FakeSession()
    bridge._started = True
    set_bridge(bridge)

    executor = MagicMock()
    executor.registry = MagicMock()
    executor.policy = MagicMock()
    monkeypatch.setattr(
        "quimera.runtime.mcp.manager.refresh_registration",
        lambda current_executor, current_bridge: captured.update(refreshed=True),
    )

    manager = MCPConnectionManager(
        executor=executor,
        workspace=workspace,
    )
    assert manager.disconnect("jira") is True

    assert captured["disconnected"] is True
    assert captured["refreshed"] is True
    assert config.mcp_clients == ["jira=stdio:jira-cmd"]
    assert manager.list_connections()[0].connected is False


def test_manager_remove_desconecta_e_apaga_specs(monkeypatch, tmp_path):
    workspace = Workspace(tmp_path)
    config = ConfigManager(workspace.mcp_config_file)
    config.set_mcp_configuration(
        ["jira=stdio:jira-cmd", "github=stdio:github-cmd"],
        ["jira=TOKEN=abc", "github=TOKEN=def"],
    )

    bridge = MCPClientBridge()
    bridge._sessions["jira"] = MagicMock()
    bridge._started = True
    set_bridge(bridge)
    monkeypatch.setattr(
        "quimera.runtime.mcp.manager.refresh_registration",
        lambda current_executor, current_bridge: [],
    )

    manager = MCPConnectionManager(
        executor=MagicMock(),
        workspace=workspace,
    )
    manager.remove("jira")

    assert config.mcp_clients == ["github=stdio:github-cmd"]
    assert config.mcp_client_env == ["github=TOKEN=def"]
    assert "jira" not in bridge.sessions


def test_bridge_replace_connection_preserva_antiga_se_novo_handshake_falha(monkeypatch):
    bridge = MCPClientBridge()
    old_session = MagicMock()
    bridge._sessions["jira"] = old_session
    bridge._started = True
    transport = MagicMock()
    transport.transport_type = "stdio"

    def fail_connect(self):
        raise ConnectionError("falhou")

    monkeypatch.setattr(MCPClientSession, "connect", fail_connect)

    with pytest.raises(ConnectionError, match="falhou"):
        bridge.replace_connection("jira", transport)

    assert bridge.sessions["jira"] is old_session
    old_session.disconnect.assert_not_called()
    transport.disconnect.assert_called_once_with()
    state = bridge.state("jira")
    assert state.phase == MCPConnectionPhase.CONNECTED
    assert state.detail == "reconexão falhou: falhou"


# Sequência de stderr que o ``mcp-remote`` realmente emite ao subir uma conexão
# remota bem-sucedida (com prefixos de timestamp/tag, ruído JSON-RPC e as linhas
# de progresso "Connected"/"Proxy"/"Local STDIO"). Reproduz o cenário das imagens
# reportadas por ALEX (github/jira).
MCP_REMOTE_SUCCESS_STDERR = [
    "[2026-07-10 14:27:30.001Z] [github] Using automatically selected callback port: 5598",
    "[2026-07-10 14:27:30.500Z] [github] Connecting to remote server: https://api.githubcopilot.com/mcp/",
    '[2026-07-10 14:27:30.900Z] [Local→Remote] {"jsonrpc":"2.0","method":"initialize"}',
    "[2026-07-10 14:27:31.100Z] [github] Local STDIO server running",
    "[2026-07-10 14:27:31.200Z] [github] Proxy established successfully between local STDIO and remote transport",
    "[2026-07-10 14:27:31.400Z] [github] Connected to remote server using StreamableHTTPClientTransport",
]


def _feed_stderr(transport, lines):
    for line in lines:
        transport._print_stderr_line(line)


def test_stderr_progress_lines_are_not_echoed_to_console(capsys):
    """Progresso do mcp-remote não deve duplicar o sucesso da camada Quimera."""
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote", "https://api.githubcopilot.com/mcp/"],
        name="github",
    )

    _feed_stderr(transport, MCP_REMOTE_SUCCESS_STDERR)

    err = capsys.readouterr().err
    assert err == ""
    assert "conectada" not in err
    assert "Local STDIO server running" not in err
    assert "Proxy established" not in err


def test_stderr_errors_are_echoed_once_per_message(capsys):
    """Erros continuam visíveis e sem duplicação por conexão."""
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote", "https://mcp.atlassian.com/v1/sse"],
        name="jira",
    )

    transport._print_stderr_line("[2026-07-10 14:30:00Z] [jira] Error: unauthorized (401)")
    transport._print_stderr_line("[2026-07-10 14:30:01Z] [jira] Error: unauthorized (401)")

    err = capsys.readouterr().err
    assert err.count("MCP stdio erro 'jira'") == 1
    assert "unauthorized (401)" in err


def test_auth_prompt_block_is_printed_once_per_url(capsys):
    """O bloco de autorização OAuth aparece uma única vez, mesmo se repetido."""
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote", "https://mcp.atlassian.com/v1/sse"],
        name="atlassian",
    )
    url = "https://mcp.atlassian.com/v1/authorize?client_id=abc&state=xyz"

    transport._print_stderr_line(f"[2026-07-10 14:31:00Z] [atlassian] {url}")
    transport._print_stderr_line(f"[2026-07-10 14:31:05Z] [atlassian] {url}")

    err = capsys.readouterr().err
    assert err.count("Autorização MCP necessária — conexão 'atlassian'") == 1
    assert err.count(url) == 1
    assert "Aguardando confirmação no navegador" in err


def test_mcp_client_bridge_registers_all_external_tools_for_native_approval(tmp_path):
    bridge = MCPClientBridge()
    bridge._sessions["jira"] = MultiFakeSession()
    bridge._started = True

    set_bridge(bridge)

    executor = ToolExecutor(
        config=ToolRuntimeConfig(
            workspace=Workspace(tmp_path),
            require_approval_for_mutations=True,
        ),
        approval_handler=None,
    )

    assert "jira_search_issue" in executor.registry.names()
    assert "jira_transition_issue" in executor.registry.names()
    assert executor.would_require_approval(
        ToolCall(name="jira_search_issue", arguments={"jql": "key = PC-1"})
    ) is True


# ── Falhas de handshake: resumo de uma linha no chat, diagnóstico no log ──

# Recorte do stderr real do ``mcp-remote`` rodando com ``$HOME`` somente
# leitura dentro do sandbox do workspace.
MCP_REMOTE_EROFS_STDERR = """[15] Using callback port derived from the server URL: 3736
[15] Discovering OAuth server configuration...
[15] [15] Connecting to remote server: https://mcp.example.test/mcp
[15] Proactive token refresh failed, falling back to the stored token
[15] Error deleting tokens.json: Error: EROFS: read-only file system, unlink '/home/u/.mcp-auth/mcp-remote-v1/0191_tokens.json'
    at async Object.unlink (node:internal/fs/promises:1058:10)
    at async deleteConfigFile (file:///home/u/.npm/_npx/3661/node_modules/mcp-remote/dist/chunk.js:28508:5)
  code: 'EROFS',
  syscall: 'open',
  path: '/home/u/.mcp-auth/mcp-remote-v1/8609_code_verifier_502b.txt'
}
[15] Fatal error: Error: EROFS: read-only file system, open '/home/u/.mcp-auth/mcp-remote-v1/8609_code_verifier_502b.txt'
    at async open (node:internal/fs/promises:637:25)
    at async Object.writeFile (node:internal/fs/promises:1239:14)
"""


class _CrashedProcess:
    """Processo que fecha stdout de imediato (servidor caiu) e deixa stderr."""

    def __init__(self, stderr_text: str) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO(stderr_text)
        self.pid = 4242

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        return 1

    def poll(self) -> int:
        return 1


def _passthrough_wrap(captured: dict):
    def fake_wrap(workspace, working_dir, cmd, *, extra_rw_paths=(), **kwargs):
        captured["extra_rw_paths"] = list(extra_rw_paths)
        return list(cmd)

    return fake_wrap


def test_summarize_stderr_escolhe_a_linha_fatal_sem_stack_trace():
    summary = summarize_stderr(MCP_REMOTE_EROFS_STDERR)

    assert summary.startswith("Fatal error: Error: EROFS: read-only file system, open")
    assert "at async" not in summary
    assert "[15]" not in summary
    assert "\n" not in summary
    assert summarize_stderr("") == ""
    assert summarize_stderr("[a] [b] only progress\n{\n}") == "only progress"
    assert len(summarize_stderr("x" * 500)) == 160
    assert summarize_stderr("x" * 500).endswith("…")


def test_stdio_handshake_failure_resume_o_motivo_e_manda_stderr_para_o_log(tmp_path):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.workspace_config_file).set_sandbox_enabled(True)
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote@0.3.2", "https://mcp.example.test/mcp"],
        env={"MCP_REMOTE_CONFIG_DIR": str(tmp_path / "auth")},
        name="jira",
        workspace=workspace,
    )
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("quimera.runtime.mcp.client")
    logger.addHandler(handler)
    try:
        with patch(
            "quimera.runtime.mcp.client.wrap_subprocess_cmd", side_effect=_passthrough_wrap({})
        ), patch(
            "quimera.runtime.mcp.client.subprocess.Popen",
            return_value=_CrashedProcess(MCP_REMOTE_EROFS_STDERR),
        ):
            with pytest.raises(MCPConnectError) as info:
                MCPClientSession(transport, name="jira").connect()
    finally:
        logger.removeHandler(handler)

    reason = str(info.value)
    assert "\n" not in reason
    assert reason.startswith(
        "MCP client: conexão fechada pelo servidor · "
        "escrita bloqueada pelo sandbox do workspace (/sandbox status) · "
        "Fatal error: Error: EROFS: read-only file system"
    )
    # A dica do sandbox sobrevive ao corte de 200 caracteres do bloco de status.
    assert "sandbox do workspace" in reason[:200]
    assert "at async" not in reason
    assert "comando:" not in reason
    assert "mcp-remote@0.3.2" in info.value.command
    assert "at async open" in info.value.stderr
    assert info.value.name == "jira"

    logged = [record.getMessage() for record in records if record.levelno == logging.ERROR]
    assert any(
        "stderr:" in message and "at async open" in message and "mcp-remote@0.3.2" in message
        for message in logged
    ), logged
    # As linhas de erro do processo também ficam no log, não só no resumo.
    assert any(
        "Proactive token refresh failed" in record.getMessage() for record in records
    )


def test_connect_mcp_client_spec_publica_apenas_o_resumo_da_falha_stdio(tmp_path):
    workspace = Workspace(tmp_path)
    bridge = MCPClientBridge()
    with patch(
        "quimera.runtime.mcp.client.wrap_subprocess_cmd", side_effect=_passthrough_wrap({})
    ), patch(
        "quimera.runtime.mcp.client.subprocess.Popen",
        return_value=_CrashedProcess(MCP_REMOTE_EROFS_STDERR),
    ):
        ok = connect_mcp_client_spec(
            bridge,
            "jira=stdio:npx -y mcp-remote@0.3.2 https://mcp.example.test/mcp",
            workspace=workspace,
        )

    assert ok is False
    state = bridge.state("jira")
    assert state.phase == MCPConnectionPhase.FAILED
    assert "\n" not in state.detail
    assert "Fatal error: Error: EROFS" in state.detail
    assert "at async" not in state.detail
    # Sandbox desligado neste workspace: sem dica de sandbox no motivo.
    assert "sandbox" not in state.detail


def test_connect_mcp_client_spec_nao_expoe_linhas_de_erro_do_stderr_no_detalhe(monkeypatch):
    """Erros intermediários do stderr vão para o log; o detalhe fica para OAuth e falha final."""
    captured = {}

    class SlowSession:
        def __init__(self, transport, name="external"):
            captured["transport"] = transport
            self._name = name

        def connect(self):
            transport = captured["transport"]
            transport._print_stderr_line(
                "[15] Proactive token refresh failed, falling back to the stored token"
            )
            transport._print_stderr_line(
                "[15] Error deleting tokens.json: Error: EROFS: read-only file system"
            )
            captured["detail_during_connect"] = bridge.state("jira").detail
            raise ConnectionError("cancelado")

        def disconnect(self):
            pass

    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", SlowSession)
    bridge = MCPClientBridge()

    assert connect_mcp_client_spec(bridge, "jira=stdio:npx -y mcp-remote https://x/mcp") is False
    assert captured["detail_during_connect"] == ""
    assert bridge.state("jira").detail == "cancelado"


# ── Sandbox: o mcp-remote precisa escrever o store OAuth ────────────────


def test_stdio_mcp_remote_libera_diretorio_de_credenciais_no_sandbox(tmp_path):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.workspace_config_file).set_sandbox_enabled(True)
    auth_dir = tmp_path / "auth"
    transport = StdioMCPTransport(
        ["npx", "-y", "mcp-remote@0.3.2", "https://mcp.example.test/mcp"],
        env={"MCP_REMOTE_CONFIG_DIR": str(auth_dir)},
        workspace=workspace,
    )
    captured: dict = {}
    process = MagicMock()
    process.stderr = None
    with patch(
        "quimera.runtime.mcp.client.wrap_subprocess_cmd", side_effect=_passthrough_wrap(captured)
    ), patch("quimera.runtime.mcp.client.subprocess.Popen", return_value=process):
        transport.connect()

    assert captured["extra_rw_paths"] == [str(auth_dir)]
    assert auth_dir.is_dir()


def test_stdio_generico_nao_ganha_excecao_e_sandbox_off_nao_cria_diretorio(tmp_path):
    workspace = Workspace(tmp_path)
    assert StdioMCPTransport(["python", "-m", "servidor"], workspace=workspace).sandbox_rw_paths() == []

    auth_dir = tmp_path / "auth"
    transport = StdioMCPTransport(
        ["npx", "mcp-remote", "https://x/mcp"],
        env={"MCP_REMOTE_CONFIG_DIR": str(auth_dir)},
        workspace=workspace,
    )
    captured: dict = {}
    process = MagicMock()
    process.stderr = None
    with patch(
        "quimera.runtime.mcp.client.wrap_subprocess_cmd", side_effect=_passthrough_wrap(captured)
    ), patch("quimera.runtime.mcp.client.subprocess.Popen", return_value=process):
        transport.connect()

    assert captured["extra_rw_paths"] == []
    assert not auth_dir.exists()


def test_remote_transport_sempre_libera_store_oauth_mesmo_com_runner_customizado(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("QUIMERA_MCP_REMOTE_CMD", "meu-proxy")
    monkeypatch.setenv("MCP_REMOTE_CONFIG_DIR", str(tmp_path / "store"))

    _, transport = parse_mcp_client_spec("jira=remote:https://mcp.example.test/mcp")

    assert isinstance(transport, RemoteMCPTransport)
    assert transport.sandbox_rw_paths() == [str(tmp_path / "store")]
    assert (tmp_path / "store").is_dir()


def test_manager_reconnect_encaminha_autorizacao_oauth_e_silencia_stderr(
    tmp_path, monkeypatch, capsys
):
    """Reconectar pelo MCP Hub segue o mesmo contrato de avisos do boot."""
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.mcp_config_file).set_mcp_configuration(
        ["jira=stdio:npx -y mcp-remote https://mcp.example.test/mcp"], []
    )
    captured: dict = {}
    url = "https://auth.example.test/authorize?x=1"

    class SlowSession:
        def __init__(self, transport, name="external"):
            captured["transport"] = transport
            self._name = name

        def connect(self):
            transport = captured["transport"]
            transport._print_stderr_line(f"[15] {url}")
            transport._print_stderr_line("[15] Error: unauthorized (401)")
            state = bridge.state("jira")
            captured["auth_url_during_connect"] = state.auth_url
            captured["detail_during_connect"] = state.detail
            raise ConnectionError("recusada")

        def disconnect(self):
            pass

    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", SlowSession)
    bridge = MCPClientBridge()
    set_bridge(bridge)
    manager = MCPConnectionManager(executor=MagicMock(), workspace=workspace)

    with pytest.raises(ConnectionError):
        manager.reconnect("jira")

    assert captured["auth_url_during_connect"] == url
    assert captured["detail_during_connect"] == ""
    assert bridge.state("jira").detail == "recusada"
    assert bridge.state("jira").auth_url == ""
    assert capsys.readouterr().err == ""


# ── Tentativas concorrentes: reconectar/desconectar com handshake preso ──


class _GateSession:
    """Sessão cujo ``connect`` bloqueia até o teste liberar ou o transporte cair."""

    instances: list["_GateSession"] = []

    def __init__(self, transport, name="external"):
        self._transport = transport
        self._name = name
        self.release = threading.Event()
        self.outcome: str = "ok"
        self.closed = False
        _GateSession.instances.append(self)

    @property
    def name(self):
        return self._name

    @property
    def transport(self):
        return self._transport

    def connect(self):
        # ``transport.disconnect`` (chamado pelo bridge ao abandonar a
        # tentativa) simula a morte do processo: o handshake "cai".
        self._transport.disconnect.side_effect = lambda: self.release.set()
        assert self.release.wait(5), "handshake nunca liberado"
        if self.outcome == "closed":
            raise ConnectionError("MCP client: conexão fechada pelo servidor")
        if self.outcome == "fail":
            raise ConnectionError("recusada")

    def disconnect(self):
        self.closed = True

    def list_tools(self):
        return []


def _gated_transport(kind: str = "stdio") -> MagicMock:
    transport = MagicMock()
    transport.transport_type = kind
    return transport


def _run_replace(bridge: MCPClientBridge, name: str, transport) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def _target():
        try:
            bridge.replace_connection(name, transport)
            result["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - registrado para o teste
            result["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, result


def test_bridge_nova_tentativa_abandona_handshake_preso(monkeypatch):
    """Reconectar durante um OAuth nunca concluído mata o processo antigo.

    A tentativa antiga recebe ``MCPConnectSuperseded`` e não publica estado;
    a nova segue e conecta.
    """
    _GateSession.instances.clear()
    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", _GateSession)
    bridge = MCPClientBridge()
    first_transport = _gated_transport()
    first_thread, first_result = _run_replace(bridge, "jira", first_transport)
    _wait_for(lambda: len(_GateSession.instances) == 1)
    first = _GateSession.instances[0]
    first.outcome = "closed"
    bridge.set_auth_url("jira", "https://auth/1", transport=first_transport)
    assert bridge.state("jira").auth_url == "https://auth/1"
    assert bridge.connecting("jira") is True

    second_transport = _gated_transport()
    second_thread, second_result = _run_replace(bridge, "jira", second_transport)
    _wait_for(lambda: len(_GateSession.instances) == 2)
    second = _GateSession.instances[1]

    # O transporte preso foi encerrado e sua thread terminou sem falha visível.
    first_thread.join(5)
    assert not first_thread.is_alive()
    first_transport.disconnect.assert_called()
    assert isinstance(first_result["error"], MCPConnectSuperseded)
    state = bridge.state("jira")
    assert state.phase == MCPConnectionPhase.CONNECTING
    assert state.auth_url == ""

    # Uma URL tardia do transporte abandonado é ignorada; a da nova vale.
    assert bridge.set_auth_url("jira", "https://auth/velha", transport=first_transport) is None
    assert bridge.state("jira").auth_url == ""
    bridge.set_auth_url("jira", "https://auth/2", transport=second_transport)
    assert bridge.state("jira").auth_url == "https://auth/2"

    second.release.set()
    second_thread.join(5)
    assert second_result.get("ok") is True
    state = bridge.state("jira")
    assert state.phase == MCPConnectionPhase.CONNECTED
    assert state.auth_url == ""
    assert bridge.sessions["jira"] is second
    assert bridge.connecting("jira") is False


def test_bridge_tentativa_superada_que_conecta_tarde_e_descartada(monkeypatch):
    """Se a tentativa antiga completar depois de superada, sua sessão não entra."""
    _GateSession.instances.clear()
    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", _GateSession)
    bridge = MCPClientBridge()
    first_transport = _gated_transport()
    first_thread, first_result = _run_replace(bridge, "jira", first_transport)
    _wait_for(lambda: len(_GateSession.instances) == 1)
    first = _GateSession.instances[0]
    # O "processo" sobrevive ao disconnect (ainda não tinha sido criado, por
    # exemplo) e o handshake antigo termina com sucesso mais tarde.
    first.outcome = "ok"

    second_transport = _gated_transport()
    second_thread, second_result = _run_replace(bridge, "jira", second_transport)
    _wait_for(lambda: len(_GateSession.instances) == 2)
    second = _GateSession.instances[1]
    first_thread.join(5)

    assert isinstance(first_result["error"], MCPConnectSuperseded)
    assert first.closed is True
    assert "jira" not in bridge.sessions

    second.release.set()
    second_thread.join(5)
    assert second_result.get("ok") is True
    assert bridge.sessions["jira"] is second


def test_bridge_disconnect_cancela_handshake_em_andamento(monkeypatch):
    _GateSession.instances.clear()
    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", _GateSession)
    bridge = MCPClientBridge()
    transport = _gated_transport()
    thread, result = _run_replace(bridge, "jira", transport)
    _wait_for(lambda: len(_GateSession.instances) == 1)
    _GateSession.instances[0].outcome = "closed"
    bridge.set_auth_url("jira", "https://auth", transport=transport)

    assert bridge.disconnect_connection("jira") is True

    thread.join(5)
    transport.disconnect.assert_called()
    assert isinstance(result["error"], MCPConnectSuperseded)
    state = bridge.state("jira")
    assert state.phase == MCPConnectionPhase.DISCONNECTED
    assert state.auth_url == ""
    assert bridge.connecting("jira") is False
    assert bridge.disconnect_connection("jira") is False


def test_bridge_shutdown_cancela_handshakes_em_andamento(monkeypatch):
    """Um mcp-remote esperando OAuth não pode sobreviver ao encerramento do app."""
    _GateSession.instances.clear()
    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", _GateSession)
    bridge = MCPClientBridge()
    transport = _gated_transport()
    thread, result = _run_replace(bridge, "jira", transport)
    _wait_for(lambda: len(_GateSession.instances) == 1)
    _GateSession.instances[0].outcome = "closed"

    bridge.shutdown()

    thread.join(5)
    transport.disconnect.assert_called()
    assert isinstance(result["error"], MCPConnectSuperseded)
    assert bridge.connecting("jira") is False


def test_connect_mcp_client_spec_trata_supersessao_sem_erro(monkeypatch, caplog):
    class Superseding:
        def __init__(self, transport, name="external"):
            self._name = name

        def connect(self):
            raise MCPConnectSuperseded("abandonado", name=self._name)

        def disconnect(self):
            pass

    monkeypatch.setattr("quimera.runtime.mcp.client.MCPClientSession", Superseding)
    bridge = MCPClientBridge()

    with caplog.at_level(logging.INFO, logger="quimera.runtime.mcp.client"):
        assert connect_mcp_client_spec(bridge, "jira=stdio:jira-cmd") is False

    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    # ``replace_connection`` converte a falha da tentativa corrente em failed;
    # aqui a exceção veio de dentro e a tentativa era a corrente, então o
    # estado é failed com o motivo — sem log de erro para o caso superado.
    assert bridge.state("jira").phase == MCPConnectionPhase.FAILED


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condição não satisfeita a tempo")


# ── Manager: reconectar/salvar sem bloquear a UI ─────────────────────


def test_manager_reconnect_in_background_usa_thread_e_marca_connecting(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.mcp_config_file).set_mcp_configuration(
        ["jira=stdio:jira-cmd"], ["jira=TOKEN=abc"]
    )
    _install_fake_session(monkeypatch)
    bridge = MCPClientBridge()
    bridge.mark_failed("jira", "recusada", transport="stdio")
    set_bridge(bridge)
    executor = MagicMock()
    executor.registry = ToolRegistry()
    manager = MCPConnectionManager(executor=executor, workspace=workspace)
    started: list[dict] = []

    class ImmediateThread:
        def __init__(self, *, target, args=(), kwargs=None, name="", daemon=False):
            started.append({"target": target, "args": args, "kwargs": kwargs, "name": name})
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    # Antes da thread rodar o estado já é "connecting" (feedback imediato).
    snapshot: list[str] = []
    bridge.subscribe(lambda: snapshot.append(bridge.state("jira").phase))
    thread = manager.reconnect_in_background("jira", thread_factory=ImmediateThread)

    assert isinstance(thread, ImmediateThread)
    assert snapshot[0] == MCPConnectionPhase.CONNECTING
    assert started[0]["name"] == "quimera-mcp-client-jira"
    assert started[0]["args"] == (bridge, "jira=stdio:jira-cmd")
    assert started[0]["kwargs"]["env_overrides"] == {"jira": {"TOKEN": "abc"}}
    assert started[0]["kwargs"]["executor"] is executor
    assert bridge.state("jira").phase == MCPConnectionPhase.CONNECTED

    with pytest.raises(KeyError):
        manager.reconnect_in_background("inexistente", thread_factory=ImmediateThread)


def test_manager_upsert_in_background_persiste_antes_de_conectar(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    _install_fake_session(monkeypatch)
    bridge = MCPClientBridge()
    set_bridge(bridge)
    executor = MagicMock()
    executor.registry = ToolRegistry()
    manager = MCPConnectionManager(executor=executor, workspace=workspace)
    threads: list[threading.Thread] = []

    class LazyThread(threading.Thread):
        def start(self):
            threads.append(self)

    info = manager.upsert_in_background(
        "jira=stdio:jira-cmd", env_spec="jira=TOKEN=abc", thread_factory=LazyThread
    )

    # Persistido e visível como pendente antes de qualquer handshake.
    assert info.name == "jira"
    assert info.connected is False
    assert info.phase == MCPConnectionPhase.PENDING
    assert ConfigManager(workspace.mcp_config_file).mcp_clients == ["jira=stdio:jira-cmd"]
    assert ConfigManager(workspace.mcp_config_file).mcp_client_env == ["jira=TOKEN=abc"]
    assert len(threads) == 1 and threads[0].daemon is True

    threads[0].run()
    assert bridge.state("jira").phase == MCPConnectionPhase.CONNECTED

    with pytest.raises(ValueError):
        manager.upsert_in_background("bad=ftp:host", thread_factory=LazyThread)
    assert ConfigManager(workspace.mcp_config_file).mcp_clients == ["jira=stdio:jira-cmd"]
    assert len(threads) == 1


# ── Vida do subprocesso stdio × thread que o criou ───────────────────────
# O bwrap --die-with-parent (PR_SET_PDEATHSIG) amarra o servidor à *thread*
# criadora. O filho abaixo reproduz o mecanismo sem bwrap: pede o mesmo
# PR_SET_PDEATHSIG=SIGKILL que o bwrap pede e fica aguardando.
_PDEATHSIG_CHILD = (
    "import ctypes, sys, time\n"
    "ctypes.CDLL(None).prctl(1, 9)\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG é específico do Linux")
def test_stdio_transport_sobrevive_ao_fim_da_thread_de_handshake():
    """Regressão: o servidor morria com SIGKILL assim que a thread curta de
    conexão (boot em background / MCP Hub) terminava, deixando a sessão viva
    com o pipe quebrado (`Broken pipe` em tools/list, `Unknown tool` depois)."""
    transport = StdioMCPTransport([sys.executable, "-c", _PDEATHSIG_CHILD], name="pdeathsig")
    outcome: dict[str, object] = {}

    def handshake() -> None:
        reader, _writer = transport.connect()
        outcome["banner"] = reader.readline()

    thread = threading.Thread(target=handshake, daemon=True)
    thread.start()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert outcome.get("banner") == "ready\n"
    try:
        time.sleep(0.5)
        assert transport._process is not None
        assert transport._process.poll() is None, "servidor morreu junto com a thread de handshake"
        assert transport._stderr_thread is not None and transport._stderr_thread.is_alive()
    finally:
        transport.disconnect()
    assert transport._process is None


def test_stdio_transport_repassa_falha_do_popen_a_thread_do_handshake(tmp_path):
    transport = StdioMCPTransport([str(tmp_path / "servidor-inexistente")], name="quebrado")
    with pytest.raises(FileNotFoundError):
        transport.connect()
    assert transport._process is None
