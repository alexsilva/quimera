"""Fluxos do MCP Hub com persistência real e transportes isolados."""

import asyncio
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
from quimera.ui.textual.mcp_screen import MCPConnectionsScreen, MCPHttpSettingsEditorScreen, MCPServerEditorScreen


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


def test_hub_toggle_changes_only_workspace_and_schedules_connection(context, monkeypatch):
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
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.entry("github").active
            assert str(screen.query_one("#mcp_toggle", Button).label) == "Desativar"
            assert connections == ["github=stdio:github"]
            await pilot.pause(0.25)
            await pilot.click("#mcp_toggle")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not store.entry("github").active
            assert screen.query_one("#mcp_reconnect", Button).disabled
            assert store.global_file.read_bytes() == before

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
