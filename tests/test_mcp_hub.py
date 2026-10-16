"""Fluxos do MCP Hub com persistência real e transportes isolados."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.app import App
from textual.widgets import Button, DataTable, Input, Label, Select

from quimera.runtime.drivers.tool_schemas import set_bridge_schemas
from quimera.runtime.mcp.client import MCPClientBridge
from quimera.runtime.mcp.manager import MCPConnectionManager
from quimera.runtime.mcp.settings import MCPConnectionStore, MCPHttpSettingsStore
from quimera.runtime.tools.mcp_clients import set_bridge
from quimera.ui.textual.mcp_screen import (
    MCPConnectionsScreen,
    MCPHttpSettingsEditorScreen,
    MCPScopePromptScreen,
    MCPServerEditorScreen,
)


@pytest.fixture
def context(tmp_path):
    workspace = SimpleNamespace(
        mcp_connections_file=tmp_path / "workspace" / "mcp-connections.json",
        global_mcp_connections_file=tmp_path / "global" / "mcp-connections.json",
        mcp_server_settings_file=tmp_path / "workspace" / "mcp-server.json",
        global_mcp_server_settings_file=tmp_path / "global" / "mcp-server.json",
        mcp_server_file=tmp_path / "state" / "mcp-server.json",
        runtime_secret_files=(),
    )
    set_bridge(MCPClientBridge())
    set_bridge_schemas([])
    app = SimpleNamespace(workspace=workspace, tool_executor=Mock(), mcp_socket_path="/tmp/quimera-test.sock")
    yield app
    set_bridge(None)
    set_bridge_schemas([])


def test_hub_local_toggle_changes_only_workspace_and_schedules_connection(context, monkeypatch):
    store = MCPConnectionStore.from_workspace(context.workspace)
    store.global_config.set_mcp_configuration(["github=stdio:github"], ["github=TOKEN=test"])
    before = store.global_file.read_bytes()
    connections = []
    monkeypatch.setattr(
        MCPConnectionManager, "_start_connection_thread",
        lambda self, spec, **kwargs: connections.append(spec),
    )

    async def run():
        app = App()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(MCPConnectionsScreen(context, app))
            await pilot.pause()
            await pilot.click("#mcp_tab_servers")
            await pilot.pause()
            screen = app.screen
            assert str(screen.query_one("#mcp_table", DataTable).get_row_at(0)[3]) == "inativo"
            assert screen.query_one("#mcp_reconnect", Button).disabled
            await pilot.click("#mcp_toggle")
            await pilot.pause()
            # O foco inicial é "Neste workspace": o caminho curto não amplia o alcance.
            assert isinstance(app.screen, MCPScopePromptScreen)
            assert app.screen.focused.id == "mcp_scope_local"
            await pilot.click("#mcp_scope_local")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.entry("github").active
            assert store.entry("github").activation_scope == "local"
            assert str(screen.query_one("#mcp_toggle", Button).label) == "Desativar"
            assert connections == ["github=stdio:github"]
            await pilot.pause(0.25)
            await pilot.click("#mcp_toggle")
            await pilot.pause()
            await pilot.click("#mcp_scope_local")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not store.entry("github").active
            assert screen.query_one("#mcp_reconnect", Button).disabled
            assert store.global_file.read_bytes() == before

    asyncio.run(run())


def test_hub_asks_scope_and_global_activation_changes_the_default(context, monkeypatch):
    store = MCPConnectionStore.from_workspace(context.workspace)
    store.global_config.set_mcp_configuration(["github=stdio:github"], None)
    connections = []
    monkeypatch.setattr(
        MCPConnectionManager, "_start_connection_thread",
        lambda self, spec, **kwargs: connections.append(spec),
    )

    async def run():
        app = App()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(MCPConnectionsScreen(context, app))
            await pilot.pause()
            await pilot.click("#mcp_tab_servers")
            await pilot.pause()
            hub = app.screen
            table = hub.query_one("#mcp_table", DataTable)
            toggle = hub.query_one("#mcp_toggle", Button)
            assert [str(cell) for cell in table.get_row_at(0)][3:] == ["inativo", "global"]

            # Esc cancela sem gravar nada.
            await pilot.click("#mcp_toggle")
            await pilot.pause()
            assert isinstance(app.screen, MCPScopePromptScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is hub and not store.global_config.mcp_active and connections == []

            # G liga o padrão global: todos os workspaces passam a ligar a conexão.
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("g")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.global_config.mcp_active == ["github"]
            assert store.workspace_config.mcp_active is None
            assert str(table.get_row_at(0)[4]) == "global"
            assert connections == ["github=stdio:github"]
            assert str(toggle.label) == "Desativar"

            # L desliga só aqui: o padrão segue ligado e a linha vira exceção local.
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("l")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.global_config.mcp_active == ["github"]
            assert store.workspace_config.mcp_inactive == ["github"]
            assert [str(cell) for cell in table.get_row_at(0)][3:] == ["inativo", "local"]
            assert "o padrão global o liga" in str(hub.query_one("#mcp_activity", Label).render())

            # O editor abre no escopo que a coluna mostra: exceção local, configuração local.
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, MCPServerEditorScreen)
            assert app.screen.query_one("#mcp_editor_scope", Select).value == "local"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is hub

            # Ativar em todos faz este workspace voltar a seguir o padrão.
            await pilot.press("ctrl+t")
            await pilot.pause()
            await pilot.press("g")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.workspace_config.mcp_inactive is None
            assert str(table.get_row_at(0)[4]) == "global"
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert app.screen.query_one("#mcp_editor_scope", Select).value == "global"
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_hub_configures_starts_and_stops_http_without_restart(context, monkeypatch):
    started = []
    shutdown = Mock()

    def start(app, workspace, settings):
        started.append(settings)
        app.external_mcp_http_server = SimpleNamespace(shutdown=shutdown, known_clients=lambda: [])
        app.mcp_http_url = settings.url
        app.external_mcp_http_url = settings.url
        app.mcp_http_settings = settings
        return app.external_mcp_http_server

    monkeypatch.setattr("quimera.ui.textual.mcp_screen.start_external_mcp_http", start)
    store = MCPHttpSettingsStore.from_workspace(context.workspace)

    async def run():
        app = App()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(MCPConnectionsScreen(context, app))
            await pilot.pause()
            hub = app.screen
            await pilot.click("#mcp_server_configure")
            await pilot.pause()
            assert isinstance(app.screen, MCPHttpSettingsEditorScreen)
            app.screen.query_one("#mcp_http_editor_port", Input).value = "9097"
            app.screen.query_one("#mcp_http_editor_allow_tools", Input).value = "agent"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is hub
            assert store.load().port == 9097 and not store.load().enabled
            await pilot.click("#mcp_server_toggle")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert started[0].port == 9097 and started[0].allow_tools == "agent"
            assert store.load().enabled
            assert context.mcp_http_url == "http://127.0.0.1:9097/mcp"
            assert str(hub.query_one("#mcp_server_toggle", Button).label) == "Desativar"
            await pilot.pause(0.25)
            await pilot.click("#mcp_server_toggle")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not store.load().enabled
            assert context.external_mcp_http_server is None
            assert context.mcp_http_url is None
            shutdown.assert_called_once_with()

    asyncio.run(run())


def test_hub_can_save_global_connection_from_editor(context, monkeypatch):
    monkeypatch.setattr(MCPConnectionManager, "_start_connection_thread", lambda *args, **kwargs: None)
    store = MCPConnectionStore.from_workspace(context.workspace)

    async def run():
        app = App()
        async with app.run_test(size=(100, 32)) as pilot:
            manager = MCPConnectionManager.from_app(context)
            app.push_screen(MCPServerEditorScreen(manager, app))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#mcp_editor_name", Input).value = "github"
            screen.query_one("#mcp_editor_scope", Select).value = "global"
            screen.query_one("#mcp_editor_transport", Select).value = "stdio"
            screen.query_one("#mcp_editor_endpoint", Input).value = "github-command"
            screen.query_one("#mcp_editor_env", Input).value = "TOKEN=test-secret"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not isinstance(app.screen, MCPServerEditorScreen)
            entry = store.entry("github")
            assert entry.is_global and entry.active
            assert entry.env_text == "TOKEN=test-secret"
            assert store.workspace_config.mcp_clients is None

    asyncio.run(run())


def test_http_editor_changes_global_defaults_and_can_restore_inheritance(context):
    store = MCPHttpSettingsStore.from_workspace(context.workspace)
    store.save("global", port=9096, allow_tools="read-local")
    store.save("local", enabled=True, port=9097, allow_tools="agent")

    async def run():
        app = App()
        async with app.run_test(size=(100, 28)) as pilot:
            app.push_screen(MCPHttpSettingsEditorScreen(store, app))
            await pilot.pause()
            editor = app.screen
            assert editor.query_one("#mcp_http_editor_port", Input).value == "9097"
            editor.query_one("#mcp_http_editor_scope", Select).value = "global"
            await pilot.pause()
            assert editor.query_one("#mcp_http_editor_port", Input).value == "9096"
            assert editor.query_one("#mcp_http_editor_allow_tools", Input).value == "read-local"
            editor.query_one("#mcp_http_editor_port", Input).value = "9098"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert store.layer("global")["port"] == 9098
            assert store.load().port == 9097
            app.push_screen(MCPHttpSettingsEditorScreen(store, app))
            await pilot.pause()
            await pilot.click("#mcp_http_editor_inherit")
            await pilot.pause()
            assert store.load().port == 9098
            assert store.load().allow_tools == "read-local"
            assert store.load().enabled
            assert store.layer("local") == {"enabled": True}

    asyncio.run(run())


def test_hub_activates_clients_per_workspace_groups_registrations_and_closes_sessions(context):
    from quimera.runtime.mcp.http_server import ConnectedMCPClient

    def client(client_id, name, **kwargs):
        base = dict(session_id="", scope="mcp", profile="", initialized=False, connected=False, authorized=True)
        return ConnectedMCPClient(client_id=client_id, client_name=name, **{**base, **kwargs})

    clients = [
        client("chatgpt-1", "ChatGPT", session_id="s1", connected=True, initialized=True),
        client("chatgpt-2", "ChatGPT"),
        client("claude-1", "Claude Code (quimera)", scope="mcp:all"),
        client("pending-1", "Pendente", authorized=False),
    ]
    revoked, closed = [], []
    context.external_mcp_http_server = SimpleNamespace(
        known_clients=lambda: clients,
        connected_clients=lambda: [c for c in clients if c.connected],
        close_client_sessions=lambda ids: closed.append(tuple(ids)) or 1,
        revoke_client_authorization=lambda client_id: revoked.append(client_id) or 1,
    )
    context.mcp_http_url = "http://127.0.0.1:9096/mcp"
    store = MCPHttpSettingsStore.from_workspace(context.workspace)
    store.set_client_active("Claude Code (quimera)", True)
    store.set_client_active("Pendente", True, "global")

    async def run():
        app = App()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(MCPConnectionsScreen(context, app))
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#mcp_incoming_table", DataTable)
            rows = [tuple(str(cell) for cell in table.get_row_at(i)) for i in range(table.row_count)]
            assert [row[0] for row in rows] == ["ChatGPT ×2", "Claude Code (quimera)", "Pendente"]
            assert [row[3] for row in rows] == ["inativo", "ativo", "não autorizado"]
            assert str(screen.query_one("#mcp_incoming_summary", Label).render()) == "2 autorizados · 1 ativos aqui"
            toggle = screen.query_one("#mcp_client_toggle", Button)
            assert str(toggle.label) == "Ativar" and not toggle.disabled
            # Os dois botões da aba cabem na tela: nada é cortado pela borda.
            for selector in ("#mcp_client_toggle", "#mcp_revoke_client"):
                region = screen.query_one(selector, Button).region
                assert region.right <= app.size.width, (selector, region, app.size)
                assert region.bottom <= app.size.height, (selector, region, app.size)

            assert [row[4] for row in rows] == ["global", "local", "global"]
            await pilot.click("#mcp_client_toggle")
            await pilot.pause()
            assert isinstance(app.screen, MCPScopePromptScreen)
            await pilot.click("#mcp_scope_local")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.client_decision("ChatGPT") == (True, "local")
            assert str(table.get_row_at(0)[3]) == "conectado"
            assert str(table.get_row_at(0)[4]) == "local"
            assert str(toggle.label) == "Desativar"
            assert closed == []

            await pilot.pause(0.25)
            await pilot.click("#mcp_client_toggle")
            await pilot.pause()
            await pilot.click("#mcp_scope_local")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.client_decision("ChatGPT") == (False, "local")
            assert store.client_decision("Claude Code (quimera)") == (True, "local")
            assert closed == [("chatgpt-1", "chatgpt-2")]
            assert str(table.get_row_at(0)[3]) == "inativo"

            # Revogar é global e alcança todos os cadastros do mesmo nome.
            await pilot.pause(0.25)
            await pilot.click("#mcp_revoke_client")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert revoked == ["chatgpt-1", "chatgpt-2"]

            # Um client sem autorização válida não pode ser liberado.
            table.move_cursor(row=2, animate=False)
            await pilot.pause()
            assert screen.query_one("#mcp_client_toggle", Button).disabled
            # As ativações locais não tocaram o padrão global, só o que o setup gravou.
            assert json.loads(store.global_file.read_text(encoding="utf-8")) == {
                "http_clients_active": ["Pendente"]
            }

    asyncio.run(run())


def test_hub_http_toggle_reflects_persisted_choice_after_boot_failure(context, monkeypatch):
    """HTTP configurado mas sem processo (porta ocupada): Desativar impede a próxima tentativa."""
    store = MCPHttpSettingsStore.from_workspace(context.workspace)
    store.save("local", enabled=True, port=9096)
    context.external_mcp_http_error = "Address already in use"
    context.external_mcp_http_server = None
    context.mcp_http_url = None
    started = []
    monkeypatch.setattr(
        "quimera.ui.textual.mcp_screen.start_external_mcp_http",
        lambda app, workspace, settings: started.append(settings) or SimpleNamespace(),
    )

    async def run():
        app = App()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(MCPConnectionsScreen(context, app))
            await pilot.pause()
            hub = app.screen
            state = str(hub.query_one("#mcp_http_state", Label).render())
            assert "falha" in state and "Desativar não sobe mais" in state
            assert str(hub.query_one("#mcp_server_toggle", Button).label) == "Desativar"

            # Corrigir a porta no editor tenta subir de novo, porque o workspace quer o servidor.
            await pilot.click("#mcp_server_configure")
            await pilot.pause()
            app.screen.query_one("#mcp_http_editor_port", Input).value = "9097"
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [item.port for item in started] == [9097]

            await pilot.pause(0.25)
            await pilot.click("#mcp_server_toggle")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not store.load().enabled
            assert context.external_mcp_http_error == ""
            assert str(hub.query_one("#mcp_server_toggle", Button).label) == "Ativar"
            assert "inativo" in str(hub.query_one("#mcp_http_state", Label).render())
            # Desativar não tenta subir de novo.
            assert len(started) == 1

    asyncio.run(run())
