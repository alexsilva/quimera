"""Tests for the Textual UI bridge/feed model."""

from unittest.mock import Mock, patch
from contextlib import contextmanager
from types import SimpleNamespace

from rich.console import Console, Group
from rich.padding import Padding

from quimera.ui.messages import AGENT_EXECUTION_STARTED_MESSAGE
from quimera.ui.textual.app import run_textual_quimera_app
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import (
    AgentLifecycleStatus,
    TextualFeedModel,
    _agent_lifecycle_payload,
)
from quimera.ui.textual.input_gate import TextualInputGate
from quimera.ui.textual.prompt_preview_screen import (
    PromptPreviewLog,
    PromptPreviewScreen,
)
from quimera.ui.textual.connection_screen import ConnectionScreen
from quimera.ui.textual.mcp_screen import MCPConnectionsScreen
from quimera.ui.textual.renderer import TextualRenderer, _TextualStatus
import quimera.ui.textual.renderables as renderables
from quimera.ui.textual.renderables import (
    _build_question_overlay,
    _build_window_overlay_payload,
    _clear_question_overlay_widget,
    _render_event,
)
from quimera.ui.textual.terminal_modes import _external_textual_window
from quimera.ui.textual.widgets import _UnifiedFeed


def test_mcp_connections_screen_mounts_and_shows_three_roles(tmp_path):
    import asyncio

    from textual.app import App
    from textual.widgets import Button, DataTable, Label

    from quimera.runtime.mcp.client import MCPClientBridge
    from quimera.runtime.mcp.http_server import ConnectedMCPClient
    from quimera.runtime.tools.mcp_clients import set_bridge

    set_bridge(MCPClientBridge())
    config_file = tmp_path / "mcp-config.json"
    config_file.write_text(
        '{"mcp_clients":["wiki=http://localhost:3100/mcp"]}',
        encoding="utf-8",
    )
    quimera_app = SimpleNamespace(
        workspace=SimpleNamespace(mcp_config_file=config_file),
        tool_executor=Mock(),
        mcp_socket_path="/tmp/quimera.sock",
        mcp_http_url="http://127.0.0.1:9090/mcp",
        external_mcp_http_server=SimpleNamespace(
            connected_clients=lambda: [
                ConnectedMCPClient(
                    session_id="session-chatgpt",
                    client_id="quimera-client-id",
                    client_name="ChatGPT",
                    scope="mcp:agent",
                    profile="agent",
                    initialized=True,
                )
            ]
        ),
    )

    async def run_test() -> None:
        app = App()
        async with app.run_test(size=(120, 34)) as pilot:
            app.push_screen(MCPConnectionsScreen(quimera_app, app))
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, MCPConnectionsScreen)
            assert "ativo" in str(screen.query_one("#mcp_socket_state", Label).render())
            http_state = screen.query_one("#mcp_http_state", Label).render()
            assert "MCP HTTP OAuth" in str(http_state)
            assert "OAuth obrigatório" not in str(http_state)
            incoming_table = screen.query_one("#mcp_incoming_table", DataTable)
            assert incoming_table.row_count == 1
            assert str(incoming_table.get_row_at(0)[3]) == "autorizado"
            assert "1 autorizados" in str(
                screen.query_one("#mcp_incoming_summary", Label).render()
            )
            table = screen.query_one("#mcp_table", DataTable)
            assert table.row_count == 1
            close_button = screen.query_one("#mcp_close_top", Button)
            assert str(close_button.label) == "×"
            assert screen.query_one("#mcp_clients_panel").display is True
            assert screen.query_one("#mcp_servers_panel").display is False
            assert screen.query_one("#mcp_revoke_client", Button).region.bottom <= app.size.height
            assert screen.query_one("#mcp_revoke_client", Button).disabled is False

            await pilot.resize_terminal(96, 18)
            await pilot.pause()
            assert screen.query_one("#mcp_close_top", Button).region.bottom <= app.size.height
            assert incoming_table.region.height >= 1
            assert screen.query_one("#mcp_revoke_client", Button).region.bottom <= app.size.height

            await pilot.click("#mcp_tab_servers")
            await pilot.pause()
            assert screen.query_one("#mcp_clients_panel").display is False
            assert screen.query_one("#mcp_servers_panel").display is True
            assert screen.query_one("#mcp_new", Button).region.bottom <= app.size.height
            assert table.region.height >= 1
            assert screen.query_one("#mcp_edit", Button).disabled is False
            assert screen.query_one("#mcp_reconnect", Button).disabled is False
            assert screen.query_one("#mcp_disconnect", Button).disabled is False
            assert screen.query_one("#mcp_remove", Button).disabled is False

            await pilot.click("#mcp_close_top")
            await pilot.pause()
            assert not isinstance(app.screen, MCPConnectionsScreen)

    asyncio.run(run_test())


def test_mcp_incoming_summary_counts_authorized_not_connected(tmp_path):
    """O resumo da aba Clientes conta apenas authorized, não connected/initialized."""
    import asyncio

    from textual.app import App
    from textual.widgets import DataTable, Label

    from quimera.runtime.mcp.client import MCPClientBridge
    from quimera.runtime.mcp.http_server import ConnectedMCPClient
    from quimera.runtime.tools.mcp_clients import set_bridge

    set_bridge(MCPClientBridge())
    config_file = tmp_path / "mcp-config.json"
    config_file.write_text('{"mcp_clients":[]}', encoding="utf-8")
    clients = [
        ConnectedMCPClient(
            session_id="",
            client_id="chatgpt",
            client_name="ChatGPT",
            scope="mcp",
            profile="",
            initialized=False,
            connected=False,
            authorized=True,
        ),
        ConnectedMCPClient(
            session_id="",
            client_id="grok",
            client_name="Grok",
            scope="mcp",
            profile="",
            initialized=False,
            connected=False,
            authorized=True,
        ),
        ConnectedMCPClient(
            session_id="sess-pending",
            client_id="pending",
            client_name="Pending",
            scope="mcp",
            profile="",
            initialized=False,
            connected=True,
            authorized=False,
        ),
    ]
    quimera_app = SimpleNamespace(
        workspace=SimpleNamespace(mcp_config_file=config_file),
        tool_executor=Mock(),
        mcp_socket_path="",
        mcp_http_url="http://127.0.0.1:9090/mcp",
        external_mcp_http_server=SimpleNamespace(
            known_clients=lambda: clients,
            connected_clients=lambda: [c for c in clients if c.connected],
        ),
    )

    async def run_test() -> None:
        app = App()
        async with app.run_test(size=(120, 34)) as pilot:
            app.push_screen(MCPConnectionsScreen(quimera_app, app))
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, MCPConnectionsScreen)
            assert "Clientes autorizados" in str(
                screen.query_one("#mcp_incoming_title", Label).render()
            )
            summary = str(screen.query_one("#mcp_incoming_summary", Label).render())
            assert summary == "2 autorizados"
            assert "conectado" not in summary

            incoming_table = screen.query_one("#mcp_incoming_table", DataTable)
            assert incoming_table.row_count == 3
            states = {str(incoming_table.get_row_at(i)[3]) for i in range(3)}
            assert states == {"autorizado", "não autorizado"}

            await pilot.resize_terminal(96, 18)
            await pilot.pause()
            assert screen.query_one("#mcp_incoming_summary", Label).region.bottom <= app.size.height

    asyncio.run(run_test())


def test_mcp_server_editor_is_separate_modal(tmp_path):
    import asyncio

    from textual.app import App
    from textual.widgets import Input, Label, Select

    from quimera.config import ConfigManager
    from quimera.ui.textual.mcp_screen import MCPServerEditorScreen

    manager = Mock()
    manager.config = ConfigManager(tmp_path / "mcp-config.json")
    info = SimpleNamespace(
        name="github",
        transport="remote",
        endpoint="https://example.test/mcp",
        connected=True,
    )

    async def run_test() -> None:
        app = App()
        async with app.run_test(size=(100, 28)) as pilot:
            app.push_screen(MCPServerEditorScreen(manager, app, info))
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, MCPServerEditorScreen)
            assert "Editar servidor MCP" in str(
                screen.query_one("#mcp_editor_title", Label).render()
            )
            assert screen.query_one("#mcp_editor_name", Input).value == "github"
            name_input = screen.query_one("#mcp_editor_name", Input)
            transport_select = screen.query_one("#mcp_editor_transport", Select)
            endpoint_input = screen.query_one("#mcp_editor_endpoint", Input)
            assert transport_select.value == "remote"
            assert (
                endpoint_input.value
                == "https://example.test/mcp"
            )
            assert name_input.region.bottom <= transport_select.region.y
            assert transport_select.region.bottom <= endpoint_input.region.y

            await pilot.click("#mcp_editor_close")
            await pilot.pause()
            assert not isinstance(app.screen, MCPServerEditorScreen)

    asyncio.run(run_test())


def _events(model: TextualFeedModel):
    return [item.event for item in model.items]


def test_textual_renderer_routes_prompt_preview_to_modal_event():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.show_prompt_preview("codex", "PROMPT FINAL:\nTeste")

    assert len(emitted) == 1
    assert emitted[0].kind == "prompt_preview"
    assert emitted[0].agent == "codex"
    assert emitted[0].payload == {
        "agent": "codex",
        "preview": "PROMPT FINAL:\nTeste",
    }


def test_textual_renderer_adds_run_metadata_to_agent_events():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    bridge.clear_agent_active = lambda _agent: None
    renderer = TextualRenderer(bridge)

    renderer.begin_agent_run(
        "codex",
        run_id="agentrun:test",
        parent_run_id="agentrun:parent",
        delegation_id="dlg-1",
        transport="delegate",
    )
    renderer.update_agent_transient("codex", "executando")
    renderer.show_message("codex", "final")
    renderer.update_agent_transient("codex", "fora da run")

    assert emitted[0].kind == "agent_update"
    assert emitted[0].payload["run_id"] == "agentrun:test"
    assert emitted[0].payload["parent_run_id"] == "agentrun:parent"
    assert emitted[0].payload["delegation_id"] == "dlg-1"
    assert emitted[0].payload["transport"] == "delegate"
    assert emitted[1].kind == "agent_message"
    assert emitted[1].payload["run_id"] == "agentrun:test"
    assert "run_id" not in emitted[2].payload


def test_textual_renderer_visual_reset_preserves_run_id():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.begin_agent_run("codex", run_id="agentrun:test", transport="chat")
    renderer.clear_agent_transient("codex")

    assert emitted[-1].kind == "visual_reset"
    assert emitted[-1].payload["run_id"] == "agentrun:test"
    assert emitted[-1].payload["transport"] == "chat"


def test_textual_renderer_muted_agent_plain_preserves_run_context():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.begin_agent_run("opencode", run_id="agentrun:opencode", transport="chat")
    renderer.show_plain("⚒ read_file README.md", agent="opencode", muted=True)

    assert emitted[-1].kind == "tool_preview"
    assert emitted[-1].agent == "opencode"
    assert emitted[-1].payload["content"] == "⚒ read_file README.md"
    assert emitted[-1].payload["run_id"] == "agentrun:opencode"
    assert emitted[-1].payload["label"]
    assert emitted[-1].payload["theme"]


def test_prompt_preview_screen_shows_content_and_closes_with_button():
    import asyncio

    from textual.app import App
    async def run_test() -> None:
        app = App()
        async with app.run_test() as pilot:
            app.push_screen(PromptPreviewScreen("codex", "PROMPT FINAL:\nTeste"))
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, PromptPreviewScreen)
            assert screen.ALLOW_SELECT is False
            assert app.ALLOW_SELECT is False
            content = screen.query_one("#prompt_preview_content", PromptPreviewLog)
            assert content.ALLOW_SELECT is False
            assert content.styles.pointer == "default"
            assert "PROMPT FINAL:" in str(content.lines[0])
            assert "Teste" in str(content.lines[1])

            await pilot.click("#prompt_preview_close")
            await pilot.pause()
            assert not isinstance(app.screen, PromptPreviewScreen)
            assert app.ALLOW_SELECT is True

    asyncio.run(run_test())


def test_prompt_preview_screen_closes_with_escape():
    import asyncio

    from textual.app import App

    async def run_test() -> None:
        app = App()
        async with app.run_test() as pilot:
            app.push_screen(PromptPreviewScreen("claude", "preview"))
            await pilot.pause()
            assert isinstance(app.screen, PromptPreviewScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, PromptPreviewScreen)

    asyncio.run(run_test())


def test_textual_feed_replaces_agent_lifecycle_with_final_message():
    model = TextualFeedModel()

    assert model.apply(TextualUiEvent("agent_lifecycle", {"status": "completed", "message": "execução concluída"}, agent="claude")) is False
    assert model.items == []

    final = TextualUiEvent("agent_message", {"content": "Oi, Alex!", "label": "Claude"}, agent="claude")
    assert model.apply(final)
    assert len(model.items) == 1
    assert model.items[0].transient is False
    assert model.items[0].event is final
    assert not model.last_change.redraw


def test_textual_feed_moves_turn_summary_after_agent_message():
    model = TextualFeedModel()
    summary = TextualUiEvent(
        "turn_summary",
        {
            "label": "Claude Sonnet",
            "total": 5,
            "ok_count": 5,
            "duration": "157.9s",
        },
        agent="claude-sonnet",
    )

    assert model.apply(summary) is False
    assert model.items == []
    assert model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {"status": "completed", "message": "execução concluída"},
            agent="claude-sonnet",
        )
    ) is False

    final = TextualUiEvent(
        "agent_message",
        {"content": "Resposta concluída.", "label": "Claude Sonnet"},
        agent="claude-sonnet",
    )
    assert model.apply(final)

    assert [event.kind for event in _events(model)] == [
        "agent_message",
        "turn_summary",
    ]
    assert model.items[-1].event is summary
    assert model.last_change.redraw is True


def test_textual_feed_discards_stale_turn_summary_when_new_run_starts():
    model = TextualFeedModel()
    summary = TextualUiEvent(
        "turn_summary",
        {"total": 1, "ok_count": 1, "duration": "1.0s"},
        agent="claude-sonnet",
    )

    assert model.apply(summary) is False
    assert model.apply(TextualUiEvent("stream_start", {}, agent="claude-sonnet"))
    assert model.apply(
        TextualUiEvent(
            "agent_message",
            {"content": "Nova resposta.", "label": "Claude Sonnet"},
            agent="claude-sonnet",
        )
    )

    assert [event.kind for event in _events(model)] == ["agent_message"]


def test_textual_feed_marks_plain_events_as_append_only():
    model = TextualFeedModel()

    event = TextualUiEvent("plain", "linha")

    assert model.apply(event) is True
    assert model.last_change.redraw is False
    assert model.last_change.appended is model.items[-1]


def test_textual_feed_marks_transient_replacement_as_redraw():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("stream_start", {"label": "Claude"}, agent="claude"))
    assert model.last_change.redraw is False

    model.apply(TextualUiEvent("stream_chunk", "Oi", agent="claude"))

    assert model.last_change.redraw is True
    assert model.last_change.appended is None


def test_textual_feed_attaches_tool_preview_to_agent_transient():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando", agent="openai"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file a.py", agent="openai"))

    assert len(model.items) == 1
    assert model.items[0].transient is True
    assert model.items[0].event.kind == "agent_update"
    assert model.items[0].event.payload["content"] == "[thinking] analisando"
    assert model.items[0].event.payload["tools"] == ["⌘ read_file a.py"]

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando mais", agent="openai"))

    assert len(model.items) == 1
    assert model.items[0].event.payload["content"] == "[thinking] analisando mais"
    assert model.items[0].event.payload["tools"] == ["⌘ read_file a.py"]


def test_textual_feed_collapses_command_start_into_completion():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] rodando", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "$ /bin/bash -lc 'pytest'", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "✓ /bin/bash -lc 'pytest'", agent="codex"))

    assert model.items[0].event.payload["tools"] == ["✓ /bin/bash -lc 'pytest'"]


def test_textual_feed_collapses_command_start_into_error_completion():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] rodando", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "$ /bin/bash -lc 'pytest'", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "✗ /bin/bash -lc 'pytest' (exit 1)", agent="codex"))

    assert model.items[0].event.payload["tools"] == ["✗ /bin/bash -lc 'pytest' (exit 1)"]


def test_textual_feed_collapses_file_edit_start_into_completion():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] editando", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "editar app.py", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "✓ editar app.py", agent="codex"))

    assert model.items[0].event.payload["tools"] == ["✓ editar app.py"]


def test_textual_feed_keeps_distinct_commands_as_separate_lines():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] rodando", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "$ ls", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "$ pwd", agent="codex"))

    assert model.items[0].event.payload["tools"] == ["$ ls", "$ pwd"]


def test_textual_feed_keeps_thinking_content_while_tools_stream():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] planejando refactor", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "$ ls", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file app.py", agent="codex"))

    assert len(model.items) == 1
    payload = model.items[0].event.payload
    assert payload["content"] == "[thinking] planejando refactor"
    assert payload["tools"] == ["$ ls", "⌘ read_file app.py"]


def test_textual_feed_preserves_tool_history_and_marks_hidden_entries():
    model = TextualFeedModel()
    model.apply(TextualUiEvent("agent_update", {"content": "pensando"}, agent="codex"))

    for index in range(14):
        model.apply(TextualUiEvent("tool_preview", f"$ tool-{index}", agent="codex"))

    payload = model.items[0].event.payload
    assert payload["tools"][0] == "⋮ +2 ferramentas anteriores"
    assert payload["tools"][1:] == [f"$ tool-{index}" for index in range(2, 14)]
    assert model._transient_tools_by_agent["codex"] == [
        f"$ tool-{index}" for index in range(14)
    ]

    model.apply(TextualUiEvent("tool_preview", "✓ tool-0", agent="codex"))

    assert model._transient_tools_by_agent["codex"][0] == "✓ tool-0"
    assert len(model._transient_tools_by_agent["codex"]) == 14


def test_textual_tool_history_indicator_is_not_rendered_as_a_tool():
    event = TextualUiEvent(
        "agent_update",
        {
            "content": "pensando",
            "tools": ["⋮ +2 ferramentas anteriores", "$ tool-atual"],
        },
        agent="codex",
    )
    console = Console(width=100, record=True)

    console.print(_render_event(event))
    rendered = console.export_text()

    assert "⋮ +2 ferramentas anteriores" in rendered
    assert "⚒ ⋮" not in rendered


def test_textual_feed_drops_lifecycle_placeholder_when_tools_start():
    model = TextualFeedModel()

    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {
                "status": "running",
                "message": AGENT_EXECUTION_STARTED_MESSAGE,
            },
            agent="codex",
        )
    )
    model.apply(TextualUiEvent("tool_preview", "usando grep", agent="codex"))

    assert len(model.items) == 1
    payload = model.items[0].event.payload
    assert model.items[0].event.kind == "agent_update"
    assert payload["content"] == ""
    assert payload["tools"] == ["usando grep"]


def test_textual_feed_does_not_treat_arbitrary_text_as_lifecycle_placeholder():
    model = TextualFeedModel()

    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {"status": "running", "message": "iniciando execucao"},
            agent="codex",
        )
    )
    model.apply(TextualUiEvent("tool_preview", "usando grep", agent="codex"))

    assert model.items[0].event.kind == "agent_lifecycle"
    assert model.items[0].event.payload["message"] == "iniciando execucao"


def test_textual_feed_clears_tool_preview_with_final_agent_message():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando", agent="openai"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file a.py", agent="openai"))
    model.apply(TextualUiEvent("agent_message", {"content": "final", "label": "OpenAI"}, agent="openai"))

    assert len(model.items) == 1
    assert model.items[0].transient is False
    assert model.items[0].event.kind == "agent_message"
    assert "tools" not in model.items[0].event.payload


def test_textual_feed_clears_tool_preview_on_failed_lifecycle_before_retry():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando", agent="openai"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file a.py", agent="openai"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {"status": "failed", "message": "falha ao comunicar; reconectando"},
            agent="openai",
        )
    )

    assert len(model.items) == 1
    assert model.items[0].transient is True
    assert model.items[0].event.kind == "agent_lifecycle"
    assert "tools" not in model.items[0].event.payload

    model.apply(TextualUiEvent("agent_update", "[thinking] nova tentativa", agent="openai"))

    assert model.items[0].event.kind == "agent_update"
    assert model.items[0].event.payload == "[thinking] nova tentativa"


def test_textual_feed_clears_tool_preview_on_completed_lifecycle():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando", agent="openai"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file a.py", agent="openai"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            _agent_lifecycle_payload("concluído", status=AgentLifecycleStatus.COMPLETED),
            agent="openai",
        )
    )

    assert model.items == []


def test_textual_feed_lifecycle_boundary_uses_status_not_message_text():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando", agent="openai"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file a.py", agent="openai"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            _agent_lifecycle_payload("concluído textual, mas ainda running", status=AgentLifecycleStatus.RUNNING),
            agent="openai",
        )
    )

    assert model.items[0].event.payload["status"] == "running"
    assert model.items[0].event.payload["tools"] == ["⌘ read_file a.py"]



def test_textual_feed_ignores_stream_abort_after_completed_lifecycle():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "delegando...", agent="claude-sonnet"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            _agent_lifecycle_payload("concluído", status=AgentLifecycleStatus.COMPLETED),
            agent="claude-sonnet",
        )
    )

    changed = model.apply(TextualUiEvent("stream_abort", {"label": "Claude Sonnet"}, agent="claude-sonnet"))

    assert changed is False
    assert model.items == []


def test_textual_feed_terminal_failed_lifecycle_removes_transient():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "executando", agent="claude"))

    assert model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            _agent_lifecycle_payload("falhou", status=AgentLifecycleStatus.FAILED),
            agent="claude",
        )
    ) is True

    assert model.items == []


def test_textual_feed_ignores_late_transients_after_failed_lifecycle():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "executando", agent="claude"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            _agent_lifecycle_payload("falhou", status=AgentLifecycleStatus.FAILED),
            agent="claude",
        )
    )

    assert model.apply(TextualUiEvent("stream_chunk", {"text": "late chunk"}, agent="claude")) is False
    assert model.apply(TextualUiEvent("tool_preview", "late tool", agent="claude")) is False
    assert model.apply(TextualUiEvent("stream_abort", {"label": "Claude"}, agent="claude")) is False

    assert model.items == []


def test_textual_feed_agent_update_starts_new_run_after_failed_lifecycle():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "executando", agent="claude"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            _agent_lifecycle_payload("falhou", status=AgentLifecycleStatus.FAILED),
            agent="claude",
        )
    )

    assert model.apply(TextualUiEvent("agent_update", "nova tentativa", agent="claude")) is True
    assert len(model.items) == 1
    assert model.items[0].event.kind == "agent_update"
    assert model.items[0].event.payload == "nova tentativa"


def test_textual_feed_uses_delegation_id_to_isolate_same_agent_runs():
    model = TextualFeedModel()

    first = {"label": "Claude", "delegation_id": "one"}
    second = {"label": "Claude", "delegation_id": "two"}

    model.apply(TextualUiEvent("stream_start", first, agent="claude-sonnet"))
    model.apply(TextualUiEvent("stream_start", second, agent="claude-sonnet"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {**first, **_agent_lifecycle_payload("concluído", status=AgentLifecycleStatus.COMPLETED)},
            agent="claude-sonnet",
        )
    )
    model.apply(TextualUiEvent("stream_chunk", {**second, "text": "ainda rodando"}, agent="claude-sonnet"))

    assert len(model.items) == 1
    assert model.items[0].event.kind == "stream_chunk"
    assert model.items[0].event.payload["delegation_id"] == "two"


def test_textual_feed_uses_run_id_to_isolate_same_agent_runs():
    model = TextualFeedModel()

    first = {"label": "Claude", "run_id": "agentrun:one", "delegation_id": "same"}
    second = {"label": "Claude", "run_id": "agentrun:two", "delegation_id": "same"}

    model.apply(TextualUiEvent("stream_start", first, agent="claude-sonnet"))
    model.apply(TextualUiEvent("stream_start", second, agent="claude-sonnet"))
    model.apply(TextualUiEvent("stream_chunk", {**first, "text": "primeiro"}, agent="claude-sonnet"))
    model.apply(TextualUiEvent("stream_chunk", {**second, "text": "segundo"}, agent="claude-sonnet"))

    assert len(model.items) == 2
    assert {item.event.payload["run_id"] for item in model.items} == {
        "agentrun:one",
        "agentrun:two",
    }


def test_textual_feed_exposes_active_transient_slots():
    model = TextualFeedModel()
    first = {"label": "Claude", "run_id": "agentrun:one"}
    second = {"label": "Claude", "run_id": "agentrun:two"}

    model.apply(TextualUiEvent("plain", "persistente"))
    model.apply(TextualUiEvent("stream_start", first, agent="claude"))
    model.apply(TextualUiEvent("stream_start", second, agent="claude"))

    assert model.has_transients is True
    assert [index for index, _ in model.transient_items()] == [1, 2]

    model.apply(TextualUiEvent("agent_message", {**first, "content": "final"}, agent="claude"))

    assert [index for index, _ in model.transient_items()] == [2]

    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {**second, **_agent_lifecycle_payload("concluído", status=AgentLifecycleStatus.COMPLETED)},
            agent="claude",
        )
    )

    assert model.has_transients is False
    assert model.transient_items() == []


def test_textual_feed_agent_message_replaces_run_id_transient():
    model = TextualFeedModel()

    payload = {"label": "Claude", "run_id": "agentrun:one"}
    model.apply(TextualUiEvent("stream_start", payload, agent="claude"))
    model.apply(TextualUiEvent("stream_chunk", {**payload, "text": "parcial"}, agent="claude"))

    final = TextualUiEvent(
        "agent_message",
        {**payload, "content": "final"},
        agent="claude",
    )
    assert model.apply(final) is True

    assert len(model.items) == 1
    assert model.items[0].transient is False
    assert model.items[0].event is final
    assert model.last_change.redraw is True


def test_textual_feed_tool_preview_preserves_run_metadata():
    model = TextualFeedModel()

    assert model.apply(
        TextualUiEvent(
            "tool_preview",
            {
                "content": "⌘ read_file foo.py",
                "label": "MCP HTTP",
                "run_id": "http:run-1",
                "transport": "mcp_http",
            },
            agent="mcp-http",
        )
    )

    assert len(model.items) == 1
    payload = model.items[0].event.payload
    assert payload["run_id"] == "http:run-1"
    assert payload["transport"] == "mcp_http"
    assert payload["tools"] == ["◇ ⌘ read_file foo.py"]


def test_textual_feed_cli_tool_preview_with_run_id_keeps_legacy_deduplication():
    model = TextualFeedModel()
    payload = {
        "label": "Codex",
        "run_id": "agentrun:codex-1",
        "transport": "chat",
    }

    model.apply(TextualUiEvent(
        "tool_preview",
        {**payload, "content": "⚒ read_file foo.py"},
        agent="codex",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**payload, "content": "✓ read_file foo.py"},
        agent="codex",
    ))

    assert model.items[0].event.payload["tools"] == ["✓ read_file foo.py"]


def test_textual_feed_keeps_cli_and_http_mcp_tool_state_isolated():
    now = [10.0]
    model = TextualFeedModel(
        clock=lambda: now[0],
        tool_preview_dwell_seconds=0.5,
    )
    cli_payload = {
        "label": "Codex",
        "run_id": "http:cli-run-name",
        "transport": "chat",
    }
    http_payload = {
        "label": "MCP HTTP",
        "run_id": "http:mcp-run",
        "transport": "mcp_http",
        "client_name": "chatgpt",
        "mcp_msg_id": "1",
    }

    model.apply(TextualUiEvent(
        "tool_preview",
        {**cli_payload, "content": "⚒ read_file foo.py"},
        agent="codex",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**http_payload, "content": "⚒ read_file foo.py"},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**cli_payload, "content": "✓ read_file foo.py"},
        agent="codex",
    ))
    model.apply(TextualUiEvent(
        "tool_state",
        {
            **http_payload,
            "msg_id": "1",
            "tool_name": "read_file",
            "status": "finished",
        },
        agent="mcp-http",
    ))

    assert len(model.items) == 2
    cli_item = next(item for item in model.items if item.event.agent == "codex")
    http_item = next(item for item in model.items if item.event.agent == "mcp-http")
    assert cli_item.event.payload["tools"] == ["✓ read_file foo.py"]
    assert http_item.event.payload["tools"] == ["◇ ✓ read_file foo.py"]

    now[0] = 10.5
    assert model.expire_tool_previews()
    assert len(model.items) == 1
    assert model.items[0].event.agent == "codex"


def test_textual_feed_http_mcp_tool_preview_uses_remote_icon_and_merges_status():
    model = TextualFeedModel()
    payload = {
        "content": "⚒ read_file foo.py",
        "label": "MCP HTTP",
        "run_id": "http:run-1",
        "transport": "mcp_http",
    }

    model.apply(TextualUiEvent("tool_preview", payload, agent="mcp-http"))
    model.apply(
        TextualUiEvent(
            "tool_preview",
            {**payload, "content": "✓ read_file foo.py"},
            agent="mcp-http",
        )
    )

    assert model.items[0].event.payload["tools"] == ["◇ ✓ read_file foo.py"]


def test_textual_feed_groups_http_mcp_tools_by_client_across_sessions_and_runs():
    model = TextualFeedModel()

    first = {
        "content": "⚒ grep_search MCP",
        "label": "🤖  mcp-http",
        "run_id": "http:run-1",
        "session_id": "session-1",
        "client_name": "chatgpt",
        "client_version": "1",
        "transport": "mcp_http",
        "mcp_msg_id": "1",
    }
    second = {
        "content": "⚒ git_status",
        "label": "🤖  mcp-http",
        "run_id": "http:run-2",
        "session_id": "session-2",
        "client_name": "chatgpt",
        "client_version": "1",
        "transport": "mcp_http",
        "mcp_msg_id": "2",
    }

    model.apply(TextualUiEvent("tool_preview", first, agent="mcp-http"))
    model.apply(TextualUiEvent("tool_preview", second, agent="mcp-http"))

    assert len(model.items) == 1
    assert model.items[0].event.payload["tools"] == [
        "◇ ⚒ grep_search MCP",
        "◇ ⚒ git_status",
    ]


def test_textual_feed_http_mcp_keeps_identical_calls_as_distinct_requests():
    model = TextualFeedModel()
    common = {
        "content": "⚒ read_file README.md",
        "label": "🤖  mcp-http",
        "client_name": "chatgpt",
        "transport": "mcp_http",
    }

    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "run_id": "http:run-1", "mcp_msg_id": "1"},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "run_id": "http:run-2", "mcp_msg_id": "1"},
        agent="mcp-http",
    ))

    assert len(model.items) == 1
    assert model.items[0].event.payload["tools"] == [
        "◇ ⚒ read_file README.md",
        "◇ ⚒ read_file README.md",
    ]


def test_textual_feed_separates_distinct_http_mcp_clients():
    model = TextualFeedModel()

    model.apply(TextualUiEvent(
        "tool_preview",
        {
            "content": "⚒ grep_search MCP",
            "run_id": "http:run-1",
            "session_id": "session-1",
            "client_name": "chatgpt",
            "transport": "mcp_http",
            "mcp_msg_id": "1",
        },
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {
            "content": "⚒ read_file README.md",
            "run_id": "http:run-2",
            "session_id": "session-2",
            "client_name": "other-client",
            "transport": "mcp_http",
            "mcp_msg_id": "2",
        },
        agent="mcp-http",
    ))

    assert len(model.items) == 2


def test_textual_feed_http_mcp_tracks_and_projects_live_summary():
    now = [10.0]
    model = TextualFeedModel(
        clock=lambda: now[0],
        tool_preview_dwell_seconds=0.5,
    )
    common = {
        "label": "🤖  mcp-http",
        "session_id": "session-1",
        "transport": "mcp_http",
    }

    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "content": "⚒ grep_search MCP", "run_id": "http:1", "mcp_msg_id": "1"},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "content": "⚒ git_status", "run_id": "http:2", "mcp_msg_id": "2"},
        agent="mcp-http",
    ))

    assert model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:1",
            "msg_id": "1",
            "tool_name": "grep_search",
            "status": "finished",
            "duration_ms": 30,
        },
        agent="mcp-http",
    ))
    payload = model.items[0].event.payload
    assert payload["tools"] == ["◇ ✓ grep_search MCP", "◇ ⚒ git_status"]
    assert payload["tool_total"] == 1
    assert payload["tool_ok_count"] == 1
    assert payload["tool_err_count"] == 0
    assert payload["tool_duration_ms"] == 30

    assert model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:2",
            "msg_id": "2",
            "tool_name": "git_status",
            "status": "failed",
            "duration_ms": 20,
        },
        agent="mcp-http",
    ))
    payload = model.items[0].event.payload
    assert payload["tools"] == ["◇ ✓ grep_search MCP", "◇ ✗ git_status"]
    assert payload["tool_total"] == 2
    assert payload["tool_ok_count"] == 1
    assert payload["tool_err_count"] == 1
    assert payload["tool_duration_ms"] == 50
    agent_key = next(iter(model._mcp_http_tool_stats_by_agent))
    assert model._mcp_http_tool_stats_by_agent[agent_key] == {
        "total": 2,
        "ok_count": 1,
        "err_count": 1,
        "duration_ms": 50,
    }

    assert not model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:2",
            "msg_id": "2",
            "tool_name": "git_status",
            "status": "failed",
        },
        agent="mcp-http",
    ))
    assert model._mcp_http_tool_stats_by_agent[agent_key]["total"] == 2

    now[0] = 10.49
    assert not model.expire_tool_previews()
    assert "tools" in model.items[0].event.payload

    now[0] = 10.5
    assert model.expire_tool_previews()
    assert model.items == []


def test_textual_feed_http_mcp_terminal_state_without_preview_expires():
    now = [10.0]
    model = TextualFeedModel(
        clock=lambda: now[0],
        tool_preview_dwell_seconds=0.5,
    )

    assert model.apply(TextualUiEvent(
        "tool_state",
        {
            "label": "MCP HTTP",
            "run_id": "http:run-1",
            "transport": "mcp_http",
            "client_name": "chatgpt",
            "msg_id": "1",
            "tool_name": "read_file",
            "status": "finished",
            "duration_ms": 20,
        },
        agent="mcp-http",
    ))

    assert model.items[0].event.payload["tools"] == ["◇ ✓ read_file"]
    assert model.items[0].event.payload["tool_total"] == 1
    now[0] = 10.5
    assert model.expire_tool_previews()
    assert model.items == []


def test_textual_feed_http_mcp_new_tool_extends_completed_burst_visibility():
    now = [10.0]
    model = TextualFeedModel(
        clock=lambda: now[0],
        tool_preview_dwell_seconds=0.5,
    )
    common = {
        "label": "🤖  mcp-http",
        "client_name": "chatgpt",
        "transport": "mcp_http",
    }

    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "content": "⚒ grep_search MCP", "run_id": "http:1", "mcp_msg_id": "1"},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:1",
            "msg_id": "1",
            "tool_name": "grep_search",
            "status": "finished",
        },
        agent="mcp-http",
    ))

    now[0] = 10.4
    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "content": "⚒ read_file README.md", "run_id": "http:2", "mcp_msg_id": "2"},
        agent="mcp-http",
    ))

    now[0] = 10.5
    assert not model.expire_tool_previews()
    assert model.items[0].event.payload["tools"] == [
        "◇ ✓ grep_search MCP",
        "◇ ⚒ read_file README.md",
    ]

    model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:2",
            "msg_id": "2",
            "tool_name": "read_file",
            "status": "finished",
        },
        agent="mcp-http",
    ))
    now[0] = 10.99
    assert not model.expire_tool_previews()
    now[0] = 11.0
    assert model.expire_tool_previews()
    assert model.items == []


def test_textual_feed_http_mcp_idle_does_not_close_block_while_tool_is_running():
    now = [10.0]
    model = TextualFeedModel(
        clock=lambda: now[0],
        tool_preview_dwell_seconds=0.5,
    )
    common = {
        "label": "🤖  mcp-http",
        "client_name": "chatgpt",
        "transport": "mcp_http",
    }

    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "content": "⚒ grep_search MCP", "run_id": "http:1", "mcp_msg_id": "1"},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:1",
            "msg_id": "1",
            "tool_name": "grep_search",
            "status": "finished",
        },
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**common, "content": "⚒ read_file README.md", "run_id": "http:2", "mcp_msg_id": "2"},
        agent="mcp-http",
    ))

    now[0] = 11.0
    assert not model.expire_tool_previews()
    assert len(model.items) == 1
    assert model.items[0].event.payload["tools"] == [
        "◇ ✓ grep_search MCP",
        "◇ ⚒ read_file README.md",
    ]

    model.apply(TextualUiEvent(
        "tool_state",
        {
            **common,
            "run_id": "http:2",
            "msg_id": "2",
            "tool_name": "read_file",
            "status": "finished",
        },
        agent="mcp-http",
    ))
    now[0] = 11.5
    assert model.expire_tool_previews()
    assert model.items == []


def test_textual_render_event_groups_mcp_http_identity_tools_and_summary():
    event = TextualUiEvent(
        "agent_update",
        {
            "content": "",
            "label": "🤖  mcp-http",
            "style": "cyan",
            "theme": "chat",
            "transport": "mcp_http",
            "tools": ["◇ ⚒ git_status"],
            "tool_total": 3,
            "tool_ok_count": 3,
            "tool_err_count": 0,
            "tool_duration_ms": 1200,
        },
        agent="mcp-http",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "☁ 🤖  mcp-http" in output
    assert "git_status" in output
    assert "3 ferramentas · 3 concluídas · 1.2s" in output


def test_textual_feed_structured_tool_preview_stays_in_same_run():
    model = TextualFeedModel()
    payload = {"label": "OpenCode", "style": "blue", "run_id": "agentrun:opencode"}

    model.apply(TextualUiEvent("stream_start", payload, agent="opencode"))
    model.apply(
        TextualUiEvent(
            "tool_preview",
            {**payload, "content": "⚒ read_file README.md"},
            agent="opencode",
        )
    )

    assert len(model.items) == 1
    assert model.items[0].event.payload["run_id"] == "agentrun:opencode"
    assert model.items[0].event.payload["tools"] == ["⚒ read_file README.md"]


def test_textual_feed_final_without_run_id_replaces_single_active_run():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("stream_start", {"label": "Claude", "run_id": "agentrun:one"}, agent="claude"))
    model.apply(TextualUiEvent("stream_chunk", {"text": "parcial", "run_id": "agentrun:one"}, agent="claude"))

    final = TextualUiEvent("agent_message", {"content": "final", "label": "Claude"}, agent="claude")
    assert model.apply(final) is True

    assert len(model.items) == 1
    assert model.items[0].event is final
    assert model.last_change.redraw is True


def test_textual_feed_visual_reset_with_run_id_clears_only_that_run():
    model = TextualFeedModel()

    first = {"label": "Claude", "run_id": "agentrun:one"}
    second = {"label": "Claude", "run_id": "agentrun:two"}
    model.apply(TextualUiEvent("stream_start", first, agent="claude"))
    model.apply(TextualUiEvent("stream_start", second, agent="claude"))

    assert len(model.items) == 2
    assert model.apply(TextualUiEvent("visual_reset", first, agent="claude")) is True

    assert len(model.items) == 1
    assert model.items[0].event.payload["run_id"] == "agentrun:two"


def test_textual_feed_visual_reset_clears_delegated_agent_transients_by_base_agent():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("plain", "persistente"))
    model.apply(TextualUiEvent("stream_start", {"label": "Claude", "delegation_id": "one"}, agent="claude"))
    model.apply(TextualUiEvent("stream_start", {"label": "Claude", "delegation_id": "two"}, agent="claude"))

    assert len(model.items) == 3

    assert model.apply(TextualUiEvent("visual_reset", agent="claude")) is True

    assert len(model.items) == 1
    assert model.items[0].event.kind == "plain"


def test_textual_feed_hydrates_restored_history():
    model = TextualFeedModel()

    changed = model.hydrate_from_history(
        [
            {"role": "human", "content": "olá"},
            {"role": "codex-gpt-5-5", "content": "feito"},
        ],
        user_label=">>>",
        agent_resolver=lambda _agent: ("blue", "Codex"),
    )

    assert changed is True
    assert model.last_change.redraw is True
    assert [item.event.kind for item in model.items] == ["user_message", "agent_message"]
    assert model.items[0].event.payload["label"] == ">>>"
    assert model.items[1].event.agent == "codex-gpt-5-5"
    assert model.items[1].event.payload["label"] == "Codex"


def test_textual_feed_clears_tool_preview_on_stream_abort():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] analisando", agent="openai"))
    model.apply(TextualUiEvent("tool_preview", "⌘ read_file a.py", agent="openai"))
    model.apply(TextualUiEvent("stream_abort", {"label": "OpenAI"}, agent="openai"))

    assert len(model.items) == 1
    assert model.items[0].event.kind == "stream_abort"
    assert "tools" not in model.items[0].event.payload


def test_textual_feed_final_message_without_transient_is_append_only():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_message", {"content": "final", "label": "Claude"}, agent="claude"))

    assert model.last_change.redraw is False
    assert model.last_change.appended is model.items[-1]


def test_textual_feed_ignores_late_completed_lifecycle_after_final_message():
    model = TextualFeedModel()

    final = TextualUiEvent("agent_message", {"content": "Oi, Alex!", "label": "Claude"}, agent="claude")
    assert model.apply(final)

    changed = model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {"status": "completed", "message": "execução concluída"},
            agent="claude",
        )
    )

    assert changed is False
    assert len(model.items) == 1
    assert model.items[0].event is final


def test_textual_feed_accepts_lifecycle_again_after_new_stream_start():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_message", {"content": "primeira", "label": "Claude"}, agent="claude"))
    model.apply(TextualUiEvent("stream_start", {"label": "Claude"}, agent="claude"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {"status": "completed", "message": "execução concluída"},
            agent="claude",
        )
    )

    assert len(model.items) == 1
    assert model.items[0].event.kind == "agent_message"


def test_textual_feed_accumulates_stream_chunk_and_replaces_with_final_message():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("stream_start", {"label": "Claude"}, agent="claude"))
    model.apply(TextualUiEvent("stream_chunk", "Oi, ", agent="claude"))
    model.apply(TextualUiEvent("stream_chunk", "Alex", agent="claude"))

    assert len(model.items) == 1
    assert model.items[0].transient is True
    assert model.items[0].event.kind == "stream_chunk"
    assert model.items[0].event.payload["content"] == "Oi, Alex"

    model.apply(TextualUiEvent("agent_message", {"content": "Oi, Alex!", "label": "Claude"}, agent="claude"))

    assert len(model.items) == 1
    assert model.items[0].transient is False
    assert model.items[0].event.kind == "agent_message"
    assert model.items[0].event.payload["content"] == "Oi, Alex!"


def test_textual_feed_preserves_other_agents_when_one_agent_finishes():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "execução concluída", agent="claude"))
    model.apply(TextualUiEvent("agent_update", "executando", agent="codex"))
    model.apply(TextualUiEvent("agent_message", {"content": "final claude", "label": "Claude"}, agent="claude"))

    events = _events(model)
    assert [event.agent for event in events] == ["claude", "codex"]
    assert events[0].kind == "agent_message"
    assert events[1].kind == "agent_update"


def test_textual_feed_ignores_interactive_question_events():
    model = TextualFeedModel()

    assert model.apply(TextualUiEvent("question", {"question": "aprovar?"})) is False
    assert model.apply(TextualUiEvent("question_clear")) is False

    assert model.items == []


def test_textual_renderer_emits_agent_lifecycle_event():
    bridge = TextualUiBridge()
    bridge.emit = Mock()
    renderer = TextualRenderer(bridge)

    renderer.show_agent_lifecycle("claude", "completed", "execução concluída")

    bridge.emit.assert_called_once()
    event = bridge.emit.call_args.args[0]
    assert event.kind == "agent_lifecycle"
    assert event.agent == "claude"
    assert event.payload["status"] == "completed"
    assert event.payload["message"] == "execução concluída"
    assert event.payload["label"] == "🤖  Claude"
    assert event.payload["style"] == "cyan"


def test_textual_renderer_emits_notification_event_outside_feed():
    bridge = TextualUiBridge()
    bridge.emit = Mock()
    renderer = TextualRenderer(bridge)

    renderer.show_notification("Resumo salvo", severity="information", timeout=4)

    bridge.emit.assert_called_once()
    event = bridge.emit.call_args.args[0]
    assert event.kind == "notification"
    assert event.payload == {
        "message": "Resumo salvo",
        "severity": "information",
        "timeout": 4,
    }


def test_textual_renderer_abort_message_stream_skips_event_after_show_message():
    bridge = TextualUiBridge()
    bridge.emit = Mock()
    renderer = TextualRenderer(bridge)

    renderer.show_message("claude-sonnet", "resposta final")
    bridge.emit.reset_mock()

    renderer.abort_message_stream("claude-sonnet")

    bridge.emit.assert_not_called()


def test_textual_renderer_abort_message_stream_emits_event_when_stream_active():
    bridge = TextualUiBridge()
    bridge.emit = Mock()
    renderer = TextualRenderer(bridge)

    renderer.start_message_stream("claude-sonnet")
    bridge.emit.reset_mock()

    renderer.abort_message_stream("claude-sonnet")

    bridge.emit.assert_called_once()
    event = bridge.emit.call_args.args[0]
    assert event.kind == "stream_abort"
    assert event.agent == "claude-sonnet"


def test_textual_status_exit_marks_success_as_completed():
    renderer = Mock()
    status = _TextualStatus(renderer, agent="openai")

    status.__exit__(None, None, None)

    renderer.update_status.assert_called_once_with(
        "openai",
        "concluído",
        status=AgentLifecycleStatus.COMPLETED,
    )


def test_textual_status_exit_marks_exception_as_failed():
    renderer = Mock()
    status = _TextualStatus(renderer, agent="openai")

    status.__exit__(RuntimeError, RuntimeError("boom"), None)

    renderer.update_status.assert_called_once_with(
        "openai",
        "falhou",
        status=AgentLifecycleStatus.FAILED,
    )


def test_textual_bridge_handles_events_synchronously_on_textual_thread():
    bridge = TextualUiBridge()
    textual_app = Mock()

    bridge.attach_textual_app(textual_app)
    event = TextualUiEvent("user_message", {"content": "revise"})
    bridge.emit(event)

    textual_app.handle_bridge_event.assert_called_once_with(event)
    textual_app.call_from_thread.assert_not_called()


def test_textual_bridge_submit_input_echoes_user_before_queueing_message():
    bridge = TextualUiBridge()
    events = []

    class TextualApp:
        def handle_bridge_event(self, event):
            events.append((event.kind, event.payload))

        def call_from_thread(self, callback, event):
            callback(event)

    app = Mock(is_agent_running=False, active_agent_stdin=None, user_name="Alex")
    bridge.attach_quimera_app(app)
    bridge.attach_textual_app(TextualApp())

    bridge.submit_input("revise")

    assert events[0][0] == "user_message"
    assert events[0][1]["content"] == "revise"
    queued = bridge.input_queue.get_nowait()
    assert queued == "revise"
    assert queued.submission_id == events[0][1]["submission_id"]
    assert events[0][1]["submission"]["status"] == "accepted"
    app.chat_lifecycle.register_submission.assert_called_once_with(queued.submission_id)


def test_textual_feed_updates_submission_inside_existing_user_turn():
    model = TextualFeedModel()
    model.apply(
        TextualUiEvent(
            "user_message",
            {
                "content": "revise",
                "submission_id": "submission:1",
                "submission": {
                    "submission_id": "submission:1",
                    "status": "accepted",
                    "elapsed_seconds": 0,
                },
            },
        )
    )

    changed = model.apply(
        TextualUiEvent(
            "submission_status",
            {
                "submission_id": "submission:1",
                "status": "queued",
                "queue_position": 2,
                "elapsed_seconds": 1,
            },
        )
    )

    assert changed is True
    assert len(model.items) == 1
    assert model.items[0].event.payload["submission"]["status"] == "queued"
    assert model.items[0].event.payload["submission"]["queue_position"] == 2


def test_textual_feed_ignores_out_of_order_submission_revision():
    model = TextualFeedModel()
    model.apply(
        TextualUiEvent(
            "user_message",
            {
                "content": "revise",
                "submission_id": "submission:1",
                "submission": {
                    "submission_id": "submission:1",
                    "status": "accepted",
                    "revision": 0,
                },
            },
        )
    )
    model.apply(
        TextualUiEvent(
            "submission_status",
            {
                "submission_id": "submission:1",
                "status": "completed",
                "revision": 2,
            },
        )
    )

    changed = model.apply(
        TextualUiEvent(
            "submission_status",
            {
                "submission_id": "submission:1",
                "status": "running",
                "revision": 1,
            },
        )
    )

    assert changed is False
    assert model.items[0].event.payload["submission"]["status"] == "completed"


def test_textual_user_turn_renders_submission_status_below_prompt():
    from io import StringIO

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    renderable = _render_event(
        TextualUiEvent(
            "user_message",
            {
                "content": "revise",
                "label": "Alex",
                "style": "green",
                "submission": {
                    "status": "queued",
                    "queue_position": 2,
                    "elapsed_seconds": 1,
                },
            },
        )
    )

    console.print(renderable)

    text = output.getvalue()
    assert text.index("revise") < text.index("1s")
    assert "na fila · posição 2" in text


def test_textual_submission_status_line_shows_only_live_elapsed_time():
    import time as _time
    from io import StringIO

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    renderable = renderables._build_submission_status_renderable(
        {
            "status": "running",
            "elapsed_seconds": 2,
            "received_monotonic": _time.monotonic() - 3.2,
        }
    )

    console.print(renderable)

    text = output.getvalue()
    assert "5s" in text
    assert "Executando" not in text


def test_textual_submission_status_line_uses_static_time_after_terminal():
    import time as _time
    from io import StringIO

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    renderable = renderables._build_submission_status_renderable(
        {
            "status": "completed",
            "elapsed_seconds": 49,
            "received_monotonic": _time.monotonic() - 120,
        }
    )

    console.print(renderable)

    text = output.getvalue()
    assert "49s" in text
    assert "Concluída" not in text


def test_textual_submission_status_note_only_for_queue_and_failure():
    assert renderables._submission_status_note({"queue_position": 3}, "queued") == "na fila · posição 3"
    assert renderables._submission_status_note({}, "queued") == "na fila"
    assert renderables._submission_status_note({"message": "Aguardando início há mais de 5s"}, "waiting") == "Aguardando início há mais de 5s"
    assert renderables._submission_status_note({"message": "boom"}, "failed") == "falhou · boom"
    assert renderables._submission_status_note({}, "failed") == "falhou"
    assert renderables._submission_status_note({"message": "x"}, "running") == ""
    assert renderables._submission_status_note({"message": "x"}, "completed") == ""


def test_textual_submission_status_line_appends_failure_reason():
    from io import StringIO

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    renderable = renderables._build_submission_status_renderable(
        {
            "status": "failed",
            "elapsed_seconds": 12,
            "message": "timeout ao iniciar agente",
        }
    )

    console.print(renderable)

    text = output.getvalue()
    assert "12s · falhou · timeout ao iniciar agente" in text


def test_textual_submission_marker_style_follows_status():
    renderables.reset_thinking_pulse()
    try:
        assert renderables._submission_marker_style(None) is None
        assert renderables._submission_marker_style({}) is None
        assert renderables._submission_marker_style({"status": "running"}) == "bold cyan"
        assert renderables._submission_marker_style({"status": "queued"}) == "bold yellow"
        assert renderables._submission_marker_style({"status": "completed"}) == "bold green"
        assert renderables._submission_marker_style({"status": "failed"}) == "bold red"
    finally:
        renderables.reset_thinking_pulse()


def test_textual_submission_marker_blinks_only_while_active():
    renderables.reset_thinking_pulse()
    try:
        renderables.advance_thinking_pulse()
        renderables.advance_thinking_pulse()
        assert renderables._submission_marker_style({"status": "running"}) == "dim cyan"
        # Estados terminais não piscam: a esfera fica sólida na cor final.
        assert renderables._submission_marker_style({"status": "completed"}) == "bold green"
        assert renderables._submission_marker_style({"status": "failed"}) == "bold red"
    finally:
        renderables.reset_thinking_pulse()


def test_textual_user_turn_sphere_uses_submission_color():
    from rich.table import Table
    from rich.text import Text

    renderables.reset_thinking_pulse()
    renderable = renderables._render_event(
        TextualUiEvent(
            "user_message",
            {
                "content": "revise",
                "label": "Alex",
                "style": "green",
                "submission": {"status": "running", "elapsed_seconds": 1},
            },
        )
    )

    def _find_sphere(node):
        if isinstance(node, Table):
            for column in node.columns:
                for cell in column._cells:
                    if isinstance(cell, Text) and cell.plain == "●":
                        return cell
                    found = _find_sphere(cell)
                    if found is not None:
                        return found
        if isinstance(node, Group):
            for child in node.renderables:
                found = _find_sphere(child)
                if found is not None:
                    return found
        return None

    sphere = _find_sphere(renderable)
    assert sphere is not None
    assert str(sphere.style) == "bold cyan"  # esfera cyan enquanto executa


def test_textual_feed_lists_active_submission_turns():
    model = TextualFeedModel()
    model.apply(
        TextualUiEvent(
            "user_message",
            {
                "content": "roda",
                "submission_id": "submission:1",
                "submission": {
                    "submission_id": "submission:1",
                    "status": "running",
                    "revision": 1,
                },
            },
        )
    )

    active = model.active_submission_items()
    assert len(active) == 1
    assert active[0][0] == 0

    model.apply(
        TextualUiEvent(
            "submission_status",
            {
                "submission_id": "submission:1",
                "status": "completed",
                "revision": 2,
            },
        )
    )

    assert model.active_submission_items() == []


def test_textual_bridge_prunes_direct_input_owned_by_finished_thread():
    import threading

    bridge = TextualUiBridge()
    owner = threading.Thread(target=bridge.begin_direct_input)
    owner.start()
    owner.join(timeout=1)

    bridge.submit_input("novo prompt")

    assert bridge.direct_input_queue.empty()
    assert bridge.input_queue.get_nowait() == "novo prompt"
    assert bridge.is_direct_input_active() is False


def test_textual_bridge_injects_input_into_active_agent_stdin():
    bridge = TextualUiBridge()
    events = []

    class TextualApp:
        def handle_bridge_event(self, event):
            events.append((event.kind, event.payload))

        def call_from_thread(self, callback, event):
            callback(event)

    stdin = Mock()
    app = Mock(is_agent_running=True, active_agent_stdin=stdin, user_name="Alex")
    bridge.attach_quimera_app(app)
    bridge.attach_textual_app(TextualApp())

    bridge.submit_input("continua")

    stdin.write.assert_called_once_with("continua\n")
    stdin.flush.assert_called_once()
    assert events[0][0] == "user_message"
    assert events[0][1]["content"] == "continua"
    assert bridge.input_queue.empty()


def test_textual_bridge_falls_back_to_queue_when_no_active_stdin():
    bridge = TextualUiBridge()
    app = Mock(is_agent_running=True, active_agent_stdin=None)
    bridge.attach_quimera_app(app)

    bridge.submit_input("proxima rodada")

    assert bridge.input_queue.get_nowait() == "proxima rodada"


def test_textual_bridge_cancel_uses_chat_lifecycle_before_agent_client():
    bridge = TextualUiBridge()
    lifecycle = Mock()
    agent_client = Mock(_agent_running=True)
    app = Mock(is_agent_running=True, chat_lifecycle=lifecycle, agent_client=agent_client)
    bridge.emit = Mock()
    bridge.attach_quimera_app(app)

    bridge.cancel_or_exit()

    lifecycle.handle_local_interrupt.assert_called_once_with()
    agent_client.cancel_active_work.assert_not_called()


def test_textual_bridge_cancel_uses_scheduler_state_for_isolated_chat_runs():
    bridge = TextualUiBridge()
    lifecycle = Mock()
    runtime_state = Mock()
    runtime_state.get_chat_outstanding_count.return_value = 2
    agent_client = Mock(_agent_running=False)
    app = SimpleNamespace(
        is_agent_running=False,
        runtime_state=runtime_state,
        chat_lifecycle=lifecycle,
        agent_client=agent_client,
    )
    bridge.emit = Mock()
    bridge.attach_quimera_app(app)

    bridge.cancel_or_exit()

    lifecycle.handle_local_interrupt.assert_called_once_with()
    agent_client.cancel_active_work.assert_not_called()
    assert bridge.input_queue.empty()
    bridge.emit.assert_not_called()


def test_textual_bridge_ctrl_c_exits_only_when_chat_is_idle():
    bridge = TextualUiBridge()
    runtime_state = Mock()
    runtime_state.get_chat_outstanding_count.return_value = 0
    agent_client = Mock(_agent_running=False)
    agent_client.has_active_work.return_value = False
    app = SimpleNamespace(
        is_agent_running=False,
        runtime_state=runtime_state,
        chat_lifecycle=Mock(),
        agent_client=agent_client,
    )
    bridge.attach_quimera_app(app)

    bridge.cancel_or_exit()

    assert bridge.input_queue.get_nowait() == "/exit"


def test_textual_bridge_does_not_exit_while_openai_thread_is_still_alive():
    bridge = TextualUiBridge()
    lifecycle = Mock()
    runtime_state = Mock()
    runtime_state.get_chat_outstanding_count.return_value = 0
    agent_client = Mock(_agent_running=False)
    agent_client.has_active_work.return_value = True
    app = SimpleNamespace(
        is_agent_running=False,
        runtime_state=runtime_state,
        chat_lifecycle=lifecycle,
        agent_client=agent_client,
    )
    bridge.attach_quimera_app(app)

    bridge.cancel_or_exit()

    lifecycle.handle_local_interrupt.assert_called_once_with()
    assert bridge.input_queue.empty()


def test_connection_screen_resolves_profile_from_system_layer_contract():
    profile = SimpleNamespace(effective_connection=Mock(return_value=SimpleNamespace()))
    profile_resolver = Mock()
    profile_resolver.get.return_value = profile
    quimera_app = SimpleNamespace(
        system_layer=SimpleNamespace(profile_resolver=profile_resolver),
    )

    screen = ConnectionScreen(quimera_app, Mock(), "openai")

    assert screen.profile is profile
    profile_resolver.get.assert_called_once_with("openai")


def test_connection_screen_fields_show_scrollbar_when_content_overflows():
    import asyncio

    from textual.app import App
    from textual.containers import VerticalScroll

    async def run_test() -> None:
        profile = SimpleNamespace(effective_connection=Mock(return_value=SimpleNamespace()))
        profile_resolver = Mock()
        profile_resolver.get.return_value = profile
        quimera_app = SimpleNamespace(
            system_layer=SimpleNamespace(profile_resolver=profile_resolver),
        )
        app = App()

        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(ConnectionScreen(quimera_app, Mock(), "openai"))
            await pilot.pause()

            fields = app.screen.query_one("#connection_fields")
            assert isinstance(fields, VerticalScroll)
            assert fields.virtual_size.height > fields.container_size.height
            assert fields.max_scroll_y > 0
            assert fields.show_vertical_scrollbar is True

    asyncio.run(run_test())


def test_connection_screen_provider_is_select_with_supported_providers():
    import asyncio

    from textual.app import App
    from textual.widgets import Select

    from quimera.profiles.base import OpenAIConnection

    async def run_test() -> None:
        connection = OpenAIConnection(provider="codexcloud")
        profile = SimpleNamespace(effective_connection=Mock(return_value=connection))
        profile_resolver = Mock()
        profile_resolver.get.return_value = profile
        quimera_app = SimpleNamespace(
            system_layer=SimpleNamespace(profile_resolver=profile_resolver),
        )
        app = App()

        async with app.run_test(size=(80, 30)) as pilot:
            app.push_screen(ConnectionScreen(quimera_app, Mock(), "openai"))
            await pilot.pause()

            provider = app.screen.query_one("#conn_provider", Select)
            assert provider.value == "codexcloud"
            assert provider._options == [
                ("OpenAI compatível", "openai_compat"),
                ("Codex Cloud", "codexcloud"),
            ]

    asyncio.run(run_test())


def test_textual_input_gate_is_active_while_textual_is_mounted():
    gate = TextualInputGate(TextualUiBridge())

    assert gate.is_active() is False


def test_textual_input_gate_returns_current_line_buffer():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)

    bridge.set_input_value("/context show")

    assert gate.get_line_buffer() == "/context show"

    bridge.set_input_value("")

    assert gate.get_line_buffer() == ""


def test_textual_toolbar_shows_interactive_prompt_contract():
    gate = TextualInputGate(TextualUiBridge())
    gate._interactive_prompt_active = True

    assert gate._build_toolbar_text() == "Enter: confirmar  |  Ctrl+C: cancelar"


def test_textual_toolbar_shows_active_agent_contract():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)
    bridge.set_agent_active("claude", "🔮 Claude Sonnet")

    text = gate._build_toolbar_text()

    assert "🔮 Claude Sonnet" in text
    assert "⚙ 🔮 Claude Sonnet" in text
    assert "Enter: injetar" not in text
    assert "Ctrl+Q: sair" not in text


def test_textual_toolbar_shows_theme_with_active_agent():
    bridge = TextualUiBridge()
    gate = TextualInputGate(
        bridge,
        toolbar_context_resolver=lambda: {"theme": "panel"},
    )
    bridge.set_agent_active("claude", "🔮 Claude Sonnet")

    text = gate._build_toolbar_text()

    assert "🔮 Claude Sonnet" in text
    assert "✨ panel" in text


def test_textual_toolbar_shows_context_without_obvious_controls():
    gate = TextualInputGate(
        TextualUiBridge(),
        toolbar_context_resolver=lambda: {"responder": "🔮 Claude", "branch": "main-ui", "theme": "chat"},
    )

    text = gate._build_toolbar_text()

    assert "🔮 Claude" in text
    assert "main-ui" in text
    assert "🤖 🔮" not in text
    assert "⎇ main-ui" in text
    assert "✨ chat" in text
    assert "Enter: enviar" not in text
    assert "Ctrl+C: interromper" not in text


def test_textual_input_gate_clears_question_overlay_after_selection_timeout():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    gate = TextualInputGate(bridge)

    result = gate.read_selection_in_terminal("Escolha", ["sim", "não"], timeout=0.001)

    assert result is None
    assert [event.kind for event in emitted] == [
        "question",
        "input_active",
        "prompt",
        "input_active",
        "question_clear",
        "prompt_clear",
    ]

    gate.set_textual_mounted(True)

    assert gate.is_active() is True

    gate.set_textual_mounted(False)

    assert gate.is_active() is False


def test_textual_input_gate_marks_approval_questions_as_permission_requests():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    gate = TextualInputGate(bridge)

    result = gate.read_approval_in_terminal("Pode executar?", "Executar? ", timeout=0.001)

    assert result is None
    question_event = emitted[0]
    assert question_event.kind == "question"
    assert question_event.payload["kind"] == "approval"
    assert question_event.payload["title"] == "Permissão solicitada"
    assert question_event.payload["options"] == [
        "y/sim = aprovar",
        "n/não = negar",
        "a/todas = aprovar todas",
    ]


def test_textual_input_gate_marks_selection_questions_as_selection_requests():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    gate = TextualInputGate(bridge)

    result = gate.read_selection_in_terminal("Escolha", ["sim", "não"], timeout=0.001)

    assert result is None
    question_event = emitted[0]
    assert question_event.kind == "question"
    assert question_event.payload["kind"] == "selection"


def test_textual_input_gate_completes_command_arguments_with_spaces():
    gate = TextualInputGate(
        TextualUiBridge(),
        command_resolver=lambda: ["/context"],
        argument_resolver=lambda command, partial: ["show", "reset"] if command == "/context" else [],
    )

    assert gate.completions_for("/context s") == ["/context show"]


def test_textual_renderer_clear_screen_emits_clear_event():
    bridge = TextualUiBridge()
    bridge.emit = Mock()
    renderer = TextualRenderer(bridge)

    renderer.clear_screen()

    bridge.emit.assert_called_once()
    assert bridge.emit.call_args.args[0].kind == "clear"


def test_textual_renderer_external_window_suspends_textual_app():
    bridge = TextualUiBridge()
    events = []

    class FakeInput:
        value = "rascunho"
        cursor_position = 0

        def focus(self):
            events.append("focus")

    class FakeTextualApp:
        @contextmanager
        def suspend(self):
            events.append("suspend")
            yield
            events.append("resume")

        def query_one(self, selector):
            if selector != "#input":
                raise LookupError(selector)
            return FakeInput()

    bridge.attach_textual_app(FakeTextualApp())
    renderer = TextualRenderer(bridge)

    with renderer.external_window("external:editor", title="Editor externo"):
        events.append("editor")

    assert events == ["suspend", "editor", "resume", "focus"]


def test_textual_renderer_external_window_resets_terminal_modes():
    bridge = TextualUiBridge()
    writes = []

    class FakeStdout:
        def write(self, value):
            writes.append(value)

        def flush(self):
            writes.append("flush")

    renderer = TextualRenderer(bridge)

    with patch("quimera.ui.textual.terminal_modes.sys.__stdout__", FakeStdout()):
        with renderer.external_window("external:editor", title="Editor externo"):
            writes.append("editor")

    text = "".join(value for value in writes if value != "flush")
    assert "\x1b[?1006l" in text
    assert "\x1b[?1003l" in text
    assert "\x1b[?2004l" in text
    assert writes.count("editor") == 1


def test_textual_renderer_cycles_theme_and_tags_agent_events():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    next_theme = renderer.cycle_theme()
    renderer.show_message("claude", "olá", render_mode="plain")

    assert next_theme == renderer.theme_name
    assert emitted[0].kind == "theme_changed"
    assert emitted[1].kind == "agent_message"
    assert emitted[1].payload["theme"] == next_theme


def test_textual_renderer_exposes_legacy_visual_methods():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.show_banner("Quimera")
    renderer.show_approval("Pode executar?")
    renderer.show_delegation("claude", "codex", task="revisar")
    renderer.show_turn_summary(
        "claude",
        {"runtime": "cli", "tools": [{"status": "ok", "duration_ms": 20}]},
    )

    assert [event.kind for event in emitted] == ["banner", "approval", "delegation", "turn_summary"]
    assert emitted[0].compact is True
    assert emitted[-1].payload["total"] == 1
    assert emitted[-1].payload["ok_count"] == 1


def test_textual_renderer_emits_structured_retry_activity():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.notify_agent_retry(
        "opencode", reason="no_response", attempt=1, limit=2,
    )

    event = emitted[-1]
    assert event.kind == "agent_activity"
    assert event.agent == "opencode"
    assert event.payload["activity"] == "retrying"
    assert event.payload["reason"] == "no_response"
    assert event.payload["message"] == "sem resposta"
    assert event.payload["attempt"] == 1
    assert event.payload["limit"] == 2


def test_textual_renderer_emits_structured_failover_activity():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.notify_agent_failover("opencode", target="claude-opus")

    event = emitted[-1]
    assert event.kind == "agent_activity"
    assert event.agent == "opencode"
    assert event.payload["activity"] == "failover"
    assert event.payload["target"] == "claude-opus"
    assert event.payload["message"] == "não respondeu"


def test_textual_renderer_show_warning_stays_free_text():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.show_warning("aviso genérico do sistema")

    event = emitted[-1]
    assert event.kind == "warning"
    assert event.payload == "aviso genérico do sistema"


def test_textual_render_event_contextualizes_agent_activity_and_tools():
    retry_event = TextualUiEvent(
        "agent_activity",
        {
            "activity": "retrying",
            "label": "OpenCode",
            "style": "magenta",
            "message": "sem resposta",
            "attempt": 1,
            "limit": 2,
        },
        agent="opencode",
    )
    tools_event = TextualUiEvent(
        "turn_summary",
        {
            "total": 3,
            "ok_count": 2,
            "err_count": 1,
            "duration": "1.2s",
            "activity_counts": {
                "inspection": 1,
                "modification": 1,
                "validation": 1,
            },
        },
        agent="opencode",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(retry_event))
    console.print(_render_event(tools_event))
    output = console.export_text()

    assert "OpenCode · sem resposta · tentativa 1/2" in output
    assert "3 ferramentas · 2 concluídas · 1 falha" in output
    assert "1 inspeção · 1 alteração · 1 validação · 1.2s" in output
    assert "OpenCode · 3 ferramentas" not in output
    assert "TOOLS:" not in output
    assert "no response, retrying" not in output


def test_textual_render_event_uses_structured_execution_control():
    from datetime import datetime, timezone
    from quimera.domain.execution import (
        ExecutionControlEvent,
        ExecutionControlSource,
        ExecutionControlStatus,
    )

    console = Console(record=True, width=80)

    console.print(
        _render_event(
            TextualUiEvent(
                "execution_control",
                ExecutionControlEvent(
                    status=ExecutionControlStatus.CANCELLED,
                    source=ExecutionControlSource.USER,
                    occurred_at=datetime(2026, 7, 19, 19, 13, 31, tzinfo=timezone.utc),
                ),
            )
        )
    )

    output = console.export_text()
    assert "Execução · cancelada pelo usuário" in output
    assert "[cancelado]" not in output


def test_textual_render_event_uses_agent_identity_for_stream_abort():
    event = TextualUiEvent(
        "stream_abort",
        {"label": "Claude Sonnet", "style": "magenta", "theme": "chat"},
        agent="claude-sonnet",
    )
    console = Console(record=True, width=80)

    console.print(_render_event(event))

    output = console.export_text()
    assert "Claude Sonnet · execução interrompida" in output
    assert "claude-sonnet interrompido" not in output


def test_textual_render_event_contextualizes_reconnection_lifecycle():
    event = TextualUiEvent(
        "agent_lifecycle",
        {
            "status": "failed",
            "message": "tentativa de reconexão",
            "label": "Codex",
            "style": "blue",
            "theme": "chat",
        },
        agent="codex",
    )
    console = Console(record=True, width=80)

    console.print(_render_event(event))

    assert "Codex · tentativa de reconexão" in console.export_text()


def test_textual_renderer_emits_delegation_chain_metadata():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.show_delegation(
        "claude",
        "codex",
        task="revisar",
        delegation_id="dlg-123",
        chain=["human", "claude", "codex"],
    )

    event = emitted[-1]
    assert event.kind == "delegation"
    assert event.payload["delegation_id"] == "dlg-123"
    assert event.payload["chain"] == ["human", "claude", "codex"]


def test_textual_render_event_shows_only_delegation_content():
    event = TextualUiEvent(
        "delegation",
        {
            "from_label": "Claude",
            "from_style": "cyan",
            "to_label": "Codex",
            "to_style": "blue",
            "task": "revisar",
            "delegation_id": "dlg-123",
            "chain": ["human", "claude", "codex"],
        },
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "Claude → Codex" in output
    assert "revisar" in output
    assert "humano > claude > codex" not in output
    assert "dlg-123" not in output
    assert "╭" not in output


def test_textual_render_event_does_not_infer_human_delegator():
    event = TextualUiEvent(
        "delegation",
        {
            "from_label": "Codex GPT 5 6 Sol",
            "from_style": "blue",
            "to_label": "OpenCode Big Pickle",
            "to_style": "magenta",
            "task": "teste visual",
            "delegation_id": "dlg-456",
            "chain": ["codex-gpt-5-6-sol", "opencode-big-pickle"],
        },
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "codex-gpt-5-6-sol > opencode-big-pickle" not in output
    assert "humano" not in output
    assert "Codex GPT 5 6 Sol" in output
    assert "OpenCode Big Pickle" in output
    assert "teste visual" in output


def test_textual_render_event_orchestrator_uses_sectioned_panel():
    event = TextualUiEvent(
        "agent_message",
        {
            "content": "Análise:\nAvaliar pedido\nExecução:\ndelegate -> codex: escrever testes\nResultado:\npronto",
            "label": "Claude",
            "style": "cyan",
            "theme": "chat",
            "render_mode": "plain",
            "orchestrator": True,
        },
        agent="claude",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "[Orquestrador] Claude" in output
    assert "Análise" in output
    assert "Execução" in output
    assert "Resultado" in output
    assert "↳ delegate -> codex: escrever testes" in output


def test_textual_render_event_limits_multiline_tool_results_by_line_count():
    event = TextualUiEvent(
        "agent_update",
        {
            "content": "executando",
            "tools": ["\n".join(f"linha {i}" for i in range(1, 13))],
            "label": "Codex",
            "style": "blue",
            "theme": "chat",
        },
        agent="codex",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "linha 9" in output
    assert "⋮ +3 linhas" in output
    assert "linha 10" not in output


def test_textual_render_event_highlights_thinking_and_styles_tools():
    event = TextualUiEvent(
        "agent_update",
        {
            "content": "analisando o código do projeto",
            "tools": [
                "⚒ git_add [\"quimera/agents/client.py\", \"quimera/app/agent_pool.py\"]",
                "✓ $ pytest",
                "✗ $ ruff check (exit 1)",
                "usando quimera_git_status",
            ],
            "label": "Codex",
            "style": "blue",
            "theme": "chat",
        },
        agent="codex",
    )
    console = Console(record=True, width=200)

    console.print(_render_event(event))
    output = console.export_text()

    assert "✻ analisando o código do projeto" in output
    assert '⚒ git_add ["quimera/agents/client.py", "quimera/app/agent_pool.py"]' in output
    assert "✓ $ pytest" in output
    assert "✗ $ ruff check (exit 1)" in output
    assert "· usando quimera_git_status" in output


def test_textual_thinking_marker_pulses_and_resets_to_base_frame():
    event = TextualUiEvent(
        "agent_update",
        {"content": "analisando", "label": "Codex", "style": "blue", "theme": "chat"},
        agent="codex",
    )

    def render() -> str:
        console = Console(record=True, width=120)
        console.print(_render_event(event))
        return console.export_text()

    try:
        assert "✻ analisando" in render()
        renderables.advance_thinking_pulse()
        assert "✽ analisando" in render()
        renderables.advance_thinking_pulse()
        assert "✳ analisando" in render()
        renderables.reset_thinking_pulse()
        assert "✻ analisando" in render()
    finally:
        renderables.reset_thinking_pulse()


def test_textual_render_event_renders_lifecycle_message_as_status_not_thinking():
    event = TextualUiEvent(
        "agent_lifecycle",
        {
            "message": "iniciando execução",
            "status": "started",
            "label": "Codex",
            "style": "blue",
            "theme": "chat",
        },
        agent="codex",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "· iniciando execução" in output
    assert "✻" not in output


def test_textual_render_event_aligns_gutter_and_draws_vertical_guide():
    event = TextualUiEvent(
        "agent_update",
        {
            "content": "analisando o projeto",
            "tools": ["✓ $ pytest", "⚒ git_add arquivos\nquimera/app.py\nquimera/cli.py"],
            "label": "Codex",
            "style": "blue",
            "theme": "chat",
        },
        agent="codex",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    lines = [line for line in console.export_text().splitlines() if line.strip()]

    header, body = lines[0], lines[1:]
    assert header.startswith("●")
    assert body, "bloco transitório deveria ter linhas de corpo"
    # Guia vertical alinhada sob o ● do header em todas as linhas do bloco.
    assert all(line.startswith("│") for line in body)
    # Ícones de pensamento e tools caem na mesma coluna do label do header.
    label_col = header.index("Codex")
    assert lines[1].index("✻") == label_col
    assert lines[2].index("✓") == label_col
    assert lines[3].index("⚒") == label_col
    # Continuações de preview ficam indentadas dentro da coluna de conteúdo.
    assert lines[4].index("quimera/app.py") == label_col + 2


def test_textual_render_event_routes_rotation_notice_as_status_line():
    event = TextualUiEvent(
        "system",
        "[rotação] congelada para claude — todo input não-prefixado irá para este agente.",
    )
    console = Console(record=True, width=120)

    console.print(_render_event(event))
    output = console.export_text()

    assert "rotação · congelada para claude" in output
    assert not output.startswith("[rotação]")


def test_textual_render_event_folds_long_tool_lines_without_ellipsis():
    long_args = "[" + ", ".join(f'"arquivo_{i}.py"' for i in range(20)) + "]"
    event = TextualUiEvent(
        "agent_update",
        {
            "content": "",
            "tools": [f"⚒ git_add {long_args}"],
            "label": "Codex",
            "style": "blue",
            "theme": "chat",
        },
        agent="codex",
    )
    console = Console(record=True, width=80)

    console.print(_render_event(event))
    output = console.export_text()

    assert "arquivo_19.py" in output
    assert "…" not in output


def test_textual_feed_merges_generic_tool_line_into_rich_preview():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] commitando", agent="opencode"))
    model.apply(TextualUiEvent("tool_preview", '⚒ git_add ["a.py", "b.py"]', agent="opencode"))
    model.apply(TextualUiEvent("tool_preview", "usando quimera_git_add", agent="opencode"))

    assert model.items[0].event.payload["tools"] == ['⚒ git_add ["a.py", "b.py"]']


def test_textual_feed_upgrades_generic_tool_line_with_rich_preview():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] commitando", agent="opencode"))
    model.apply(TextualUiEvent("tool_preview", "usando quimera_git_add", agent="opencode"))
    model.apply(TextualUiEvent("tool_preview", '⚒ git_add ["a.py", "b.py"]', agent="opencode"))

    assert model.items[0].event.payload["tools"] == ['⚒ git_add ["a.py", "b.py"]']


def test_textual_feed_keeps_distinct_rich_calls_of_same_tool():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("agent_update", "[thinking] lendo", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "⚒ read_file a.py", agent="codex"))
    model.apply(TextualUiEvent("tool_preview", "⚒ read_file b.py", agent="codex"))

    assert model.items[0].event.payload["tools"] == ["⚒ read_file a.py", "⚒ read_file b.py"]


def test_textual_feed_replaces_delegate_preview_with_delegation_card():
    model = TextualFeedModel()
    task = "Delegação simples recebida e concluída."

    model.apply(TextualUiEvent("agent_update", "[thinking] delegando", agent="codex"))
    model.apply(
        TextualUiEvent(
            "tool_preview",
            f"⚒ delegate opencode-big-pickle verifier {task} False",
            agent="codex",
        )
    )
    model.apply(
        TextualUiEvent(
            "delegation",
            {
                "from_label": "Codex",
                "to_label": "OpenCode Big Pickle",
                "task": task,
                "delegation_id": "dlg-123",
            },
        )
    )

    assert "tools" not in model.items[0].event.payload
    assert model.items[1].event.kind == "delegation"
    assert model.last_change.redraw is True


def _delegation_event(delegation_id="dlg-1", task="revisar patch"):
    """Cria evento de delegação equivalente ao emitido pelo renderer Textual."""
    return TextualUiEvent(
        "delegation",
        {
            "from_label": "Codex",
            "to_label": "Sonnet",
            "task": task,
            "delegation_id": delegation_id,
            "chain": ["codex", "sonnet"],
        },
    )


def test_textual_feed_delegation_is_transient_artifact():
    model = TextualFeedModel()

    model.apply(_delegation_event())

    assert [item.event.kind for item in model.items] == ["delegation"]
    assert model.items[0].transient is True


def test_textual_feed_delegated_feed_stays_below_its_delegation():
    model = TextualFeedModel()
    delegated = {"label": "Sonnet", "run_id": "run-b", "delegation_id": "dlg-1"}

    model.apply(TextualUiEvent("agent_update", {"label": "Codex", "content": "delegando"}, agent="codex"))
    model.apply(_delegation_event())
    model.apply(TextualUiEvent("agent_update", {"label": "Qwen", "content": "outro turno"}, agent="qwen"))
    model.apply(TextualUiEvent("stream_start", delegated, agent="sonnet"))
    model.apply(TextualUiEvent("stream_chunk", {**delegated, "text": "lendo arquivos"}, agent="sonnet"))

    kinds = [item.event.kind for item in model.items]
    agents = [item.event.agent for item in model.items]
    assert kinds[1] == "delegation"
    assert agents[2] == "sonnet"
    assert agents[3] == "qwen"


def test_textual_feed_removes_delegation_group_on_visual_reset():
    model = TextualFeedModel()
    delegated = {"label": "Sonnet", "run_id": "run-b", "delegation_id": "dlg-1"}

    model.apply(_delegation_event())
    model.apply(TextualUiEvent("stream_start", delegated, agent="sonnet"))
    model.apply(TextualUiEvent("stream_chunk", {**delegated, "text": "trabalhando"}, agent="sonnet"))
    changed = model.apply(TextualUiEvent("visual_reset", delegated, agent="sonnet"))

    assert changed is True
    assert model.items == []
    assert model.has_transients is False


def test_textual_feed_delegated_final_message_is_not_persisted():
    model = TextualFeedModel()
    delegated = {"label": "Sonnet", "run_id": "run-b", "delegation_id": "dlg-1"}

    model.apply(TextualUiEvent("user_message", {"content": "faz isso", "label": ">>>"}))
    model.apply(_delegation_event())
    model.apply(TextualUiEvent("stream_start", delegated, agent="sonnet"))
    model.apply(TextualUiEvent("agent_message", {**delegated, "content": "feito"}, agent="sonnet"))

    assert [item.event.kind for item in model.items] == ["user_message"]


def test_textual_feed_removes_delegation_group_on_final_lifecycle():
    model = TextualFeedModel()
    delegated = {"label": "Sonnet", "run_id": "run-b", "delegation_id": "dlg-1"}

    model.apply(_delegation_event())
    model.apply(TextualUiEvent("stream_start", delegated, agent="sonnet"))
    model.apply(
        TextualUiEvent(
            "agent_lifecycle",
            {**delegated, **_agent_lifecycle_payload("concluído", status=AgentLifecycleStatus.COMPLETED)},
            agent="sonnet",
        )
    )

    assert model.items == []


def test_textual_feed_keeps_regular_agent_message_persistent():
    model = TextualFeedModel()

    model.apply(TextualUiEvent("stream_start", {"label": "Codex"}, agent="codex"))
    model.apply(TextualUiEvent("agent_message", {"label": "Codex", "content": "resposta"}, agent="codex"))

    assert [item.event.kind for item in model.items] == ["agent_message"]
    assert model.items[0].transient is False


def test_transient_overlay_replace_reads_previous_lines_when_executed():
    from quimera.ui.overlay import TransientOverlay

    lines = [1]
    overlay = TransientOverlay(lines)
    audits = []

    replace = overlay.build_replace(
        "novo",
        version=1,
        get_version_fn=lambda: 1,
        audit_fn=lambda event, **payload: audits.append((event, payload)),
    )
    lines[0] = 4

    class FakeStdout:
        def write(self, _value):
            return None

        def flush(self):
            return None

    with patch("quimera.ui.overlay.sys.stdout", FakeStdout()):
        replace()

    assert audits[0][0] == "transient_replace"
    assert audits[0][1]["prev_lines"] == 4


def test_textual_renderer_formats_agent_error_metadata():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.show_error("raw", agent="claude", error_kind="agent_invalid_output")

    assert emitted[-1].kind == "error"
    assert emitted[-1].agent == "claude"
    assert "não retornou saída válida" in emitted[-1].payload


def test_textual_feed_visual_reset_clears_only_transients():
    model = TextualFeedModel()
    model.apply(TextualUiEvent("plain", "persistente"))
    model.apply(TextualUiEvent("agent_update", "rodando", agent="claude"))

    assert model.apply(TextualUiEvent("visual_reset")) is True

    assert [item.event.kind for item in model.items] == ["plain"]


def test_textual_render_event_varies_agent_theme_shape():
    
    panel_event = TextualUiEvent(
        "agent_message",
        {"content": "olá", "label": "Claude", "style": "cyan", "theme": "panel", "render_mode": "plain"},
        agent="claude",
    )
    chat_event = TextualUiEvent(
        "agent_message",
        {"content": "olá", "label": "Claude", "style": "cyan", "theme": "chat", "render_mode": "plain"},
        agent="claude",
    )

    assert isinstance(_render_event(panel_event), Group)
    assert isinstance(_render_event(chat_event), Group)


def test_textual_agent_lifecycle_renders_in_chat_theme_not_panel():
    from rich.panel import Panel

    event = TextualUiEvent(
        "agent_lifecycle",
        {"message": "[dim]conectando qwen3.5-32k...[/dim]", "label": "Qwen", "style": "cyan", "theme": "chat"},
        agent="qwen3-5-9b",
    )

    rendered = _render_event(event)

    assert rendered is not None
    assert not isinstance(rendered, Panel)
    assert "[dim]" not in str(rendered)
    assert "[/dim]" not in str(rendered)


def test_textual_approval_event_renders_as_compact_line_not_panel():
    from rich.panel import Panel

    event = TextualUiEvent(
        "approval",
        "\nAprovar git_commit :: risco: write\norigem: opencode-big-pickle\nmessage: fix something",
    )

    rendered = _render_event(event)

    assert rendered is not None
    assert not isinstance(rendered, Panel)
    console = Console(record=True, width=120)
    console.print(rendered)
    output = console.export_text()
    assert "⚠" in output
    assert "git_commit :: risco: write" in output
    assert "opencode-big-pickle" in output


def test_textual_renderer_interactive_windows_emit_semantic_overlay_events():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    with renderer.approval_window(owner="claude", metadata={"question": "Executar shell?"}):
        pass
    with renderer.input_window(owner="codex"):
        pass
    with renderer.selection_window(owner="opencode"):
        pass

    assert [event.kind for event in emitted] == ["window_open", "window_clear"]
    assert emitted[0].payload["kind"] == "approval"
    assert emitted[0].payload["title"] == "Permissão solicitada"
    assert emitted[0].payload["question"] == "Executar shell?"
    assert "y/sim = aprovar" in emitted[0].payload["options"]
    assert _build_window_overlay_payload(emitted[0].payload) == {
        "question": "Executar shell?",
        "options": [
            "y/sim = aprovar",
            "n/não = negar",
            "a/todas = aprovar todas",
        ],
        "title": "Permissão solicitada",
        "kind": "approval",
        "owner": "claude",
    }


def test_textual_renderer_interactive_input_window_with_question_emits_overlay_events():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    with renderer.input_window(owner="codex", metadata={"question": "Informe o comando"}):
        pass

    assert [event.kind for event in emitted] == ["window_open", "window_clear"]
    assert emitted[0].payload["kind"] == "input"
    assert emitted[0].payload["question"] == "Informe o comando"


def test_textual_renderer_selection_window_preserves_question_and_options():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    with renderer.selection_window(
        owner="opencode",
        metadata={"question": "Escolha uma opção", "options": ["sim", "não"]},
    ):
        pass

    assert [event.kind for event in emitted] == ["window_open", "window_clear"]
    assert emitted[0].payload["kind"] == "selection"
    assert emitted[0].payload["question"] == "Escolha uma opção"
    assert emitted[0].payload["options"] == ["sim", "não"]
    assert _build_window_overlay_payload(emitted[0].payload)["options"] == ["sim", "não"]


def test_textual_approval_overlay_renders_title_question_and_options():
    renderable = _build_question_overlay(
        {
            "kind": "approval",
            "title": "Permissão solicitada",
            "question": "Executar comando via shell?",
            "options": [
                "y/sim = aprovar",
                "n/não = negar",
            ],
        }
    )
    console = Console(width=80, record=True, force_terminal=False)

    console.print(renderable)
    output = console.export_text()

    assert "Permissão solicitada" in output
    assert "Executar comando via shell?" in output
    assert "y/sim = aprovar" in output
    assert "n/não = negar" in output


def test_textual_selection_overlay_renders_numbered_options():
    renderable = _build_question_overlay(
        {
            "kind": "selection",
            "title": "Seleção solicitada",
            "question": "Escolha uma opção",
            "options": ["sim", "não"],
        }
    )
    console = Console(width=80, record=True, force_terminal=False)

    console.print(renderable)
    output = console.export_text()

    assert "Seleção solicitada" in output
    assert "Escolha uma opção" in output
    assert "1. sim" in output
    assert "2. não" in output


def test_textual_clear_question_overlay_widget_hides_approval_overlay():
    class FakeOverlay:
        display = True

        def __init__(self):
            self.value = "conteúdo anterior"

        def update(self, value):
            self.value = value

    overlay = FakeOverlay()

    _clear_question_overlay_widget(overlay)

    assert overlay.value == ""
    assert overlay.display is False


def test_textual_renderer_interactive_window_routes_answers_away_from_active_agent():
    bridge = TextualUiBridge()
    renderer = TextualRenderer(bridge)

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            self.writes.append("flush")

    stdin = FakeStdin()
    bridge.attach_quimera_app(
        SimpleNamespace(is_agent_running=True, active_agent_stdin=stdin)
    )

    with renderer.approval_window(owner="claude"):
        bridge.submit_input("a")

    assert bridge.direct_input_queue.get_nowait() == "a"
    assert stdin.writes == []


def test_textual_feed_ignores_interactive_window_events():
    model = TextualFeedModel()

    assert model.apply(TextualUiEvent("window_open", {"kind": "approval"})) is False
    assert model.apply(TextualUiEvent("window_clear", {"kind": "approval"})) is False
    assert model.items == []


def test_textual_feed_ignores_theme_changed_events():
    model = TextualFeedModel()

    assert model.apply(TextualUiEvent("theme_changed", {"theme": "panel"})) is False
    assert model.items == []


def test_textual_bridge_routes_exit_to_app_even_when_agent_is_active():
    from types import SimpleNamespace

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            self.writes.append("flush")

    stdin = FakeStdin()
    bridge = TextualUiBridge()
    bridge.attach_quimera_app(
        SimpleNamespace(is_agent_running=True, active_agent_stdin=stdin)
    )

    bridge.submit_input("/exit ")

    assert bridge.input_queue.get_nowait() == "/exit"
    assert stdin.writes == []


def test_textual_bridge_echoes_regular_user_message_to_feed():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    bridge.attach_quimera_app(SimpleNamespace(user_name="Alex"))

    bridge.submit_input("oi agente")

    assert bridge.input_queue.get_nowait() == "oi agente"
    assert emitted[-1].kind == "user_message"
    assert emitted[-1].payload["content"] == "oi agente"
    assert emitted[-1].payload["label"] == "Alex"


def test_textual_renderer_routes_connection_config_to_modal_event():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    assert renderer.open_connection_config("openai", advanced=True) is True

    assert emitted[-1].kind == "open_connection_config"
    assert emitted[-1].agent == "openai"
    assert emitted[-1].payload == {"agent": "openai", "advanced": True}


def test_textual_bridge_does_not_echo_slash_command_as_user_message():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append

    bridge.submit_input("/agents")

    assert bridge.input_queue.get_nowait() == "/agents"
    assert emitted == []


def test_textual_bridge_echoes_debate_topic_but_not_control_commands():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    bridge.attach_quimera_app(SimpleNamespace(user_name="Alex"))

    bridge.submit_input('/debate --mode workflow "planejar entrega"')
    assert emitted[-1].kind == "user_message"
    assert emitted[-1].payload["content"] == "planejar entrega"

    emitted.clear()
    bridge.submit_input("/debate status")
    assert emitted == []


def test_textual_bridge_echoes_agent_prefixed_prompt_without_prefix():
    bridge = TextualUiBridge()
    emitted = []

    def capture(event):
        assert bridge.input_queue.empty()
        emitted.append(event)

    bridge.emit = capture
    profile = SimpleNamespace(
        prefix="/opencode-big-pickle",
        aliases=["o/opencode-big-pickle"],
    )
    bridge.attach_quimera_app(
        SimpleNamespace(
            user_name="Alex",
            get_active_agent_profiles=lambda: [profile],
        )
    )

    bridge.submit_input("/opencode-big-pickle comitar")

    assert bridge.input_queue.get_nowait() == "/opencode-big-pickle comitar"
    assert len(emitted) == 1
    assert emitted[0].kind == "user_message"
    assert emitted[0].payload["content"] == "comitar"
    assert emitted[0].payload["label"] == "Alex"


def test_textual_user_message_renders_as_chat_turn():
    rendered = _render_event(TextualUiEvent("user_message", {"content": "oi", "label": "Alex"}))

    # O espaçamento entre mensagens é uniforme via CSS (.feed-entry), então o
    # turno do usuário não recebe Padding especial próprio.
    assert not isinstance(rendered, Padding)
    console = Console(width=60, record=True)
    console.print(rendered)
    output = console.export_text()
    # O prompt do usuário fica na mesma linha do nome: `● Alex: oi`.
    first_line = output.splitlines()[0]
    assert "Alex: oi" in first_line
    assert "●" in first_line


def test_textual_feed_entries_have_uniform_spacing():
    from quimera.ui.textual.styles import TEXTUAL_APP_CSS

    entry_css = TEXTUAL_APP_CSS.split(".feed-entry {", 1)[1].split("}", 1)[0]

    assert "margin-bottom: 1;" in entry_css


def test_textual_feed_compact_entries_have_no_spacing():
    from quimera.ui.textual.styles import TEXTUAL_APP_CSS

    compact_css = TEXTUAL_APP_CSS.split(".feed-entry.-compact {", 1)[1].split("}", 1)[0]

    assert "margin-bottom: 0;" in compact_css


def test_textual_only_boot_events_are_compact():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.show_banner("Quimera")
    renderer.show_boot_message("Projeto: /tmp/projeto")
    renderer.show_system_neutral("mensagem neutra durante o chat")
    renderer.show_system("mensagem de sistema durante o chat")
    renderer.show_plain("mensagem simples durante o chat")

    assert [event.compact for event in emitted] == [True, True, False, False, False]


def test_textual_restored_and_live_messages_share_normal_spacing():
    model = TextualFeedModel()
    assert model.hydrate_from_history(
        [
            {"role": "user", "content": "mensagem antiga"},
            {"role": "claude", "content": "resposta antiga"},
        ]
    )
    model.apply(TextualUiEvent("user_message", {"content": "mensagem nova"}))
    model.apply(
        TextualUiEvent(
            "agent_message",
            {"content": "resposta nova", "label": "Claude"},
            agent="claude",
        )
    )

    assert all(item.event.compact is False for item in model.items)


def test_textual_unified_feed_applies_compact_class_per_entry():
    import asyncio

    from textual.app import App, ComposeResult

    class FeedApp(App):
        def compose(self) -> ComposeResult:
            yield _UnifiedFeed(id="feed")

    async def run_test() -> None:
        app = FeedApp()
        async with app.run_test(size=(60, 16)) as pilot:
            feed = app.query_one("#feed", _UnifiedFeed)
            feed.sync_entries(
                [
                    (1, False, "Projeto: /tmp/projeto", True),
                    (2, False, "resposta de agente", False),
                ]
            )
            await pilot.pause()

            slots = list(feed.children)
            assert slots[0].has_class("-compact")
            assert not slots[1].has_class("-compact")

            # Reuso do slot por outro tipo de entrada atualiza a classe.
            feed.sync_entries(
                [
                    (3, False, "resposta de agente", False),
                    (4, False, "MCP interno iniciado", True),
                ]
            )
            await pilot.pause()

            assert list(feed.children) == slots
            assert not slots[0].has_class("-compact")
            assert slots[1].has_class("-compact")

    asyncio.run(run_test())


def test_textual_feed_reserves_at_least_ten_lines_for_agent_output():
    from quimera.ui.textual.styles import TEXTUAL_APP_CSS

    css = TEXTUAL_APP_CSS

    assert "#main" in css
    assert "min-height: 14;" in css
    assert "#feed" in css
    assert "min-height: 10;" in css
    assert "#question_overlay" in css
    assert "max-height: 12;" in css
    assert "overflow-y: auto;" in css
    assert "#input_bar" in css
    assert "max-height: 3;" in css


def test_textual_feed_uses_single_scrollable_area_without_transient_overlay():
    from quimera.ui.textual.styles import TEXTUAL_APP_CSS

    assert "#feed_transient" not in TEXTUAL_APP_CSS
    assert ".feed-entry" in TEXTUAL_APP_CSS


def test_textual_unified_feed_replaces_parallel_runs_in_their_original_slots():
    import asyncio

    from textual.app import App, ComposeResult

    class FeedApp(App):
        def compose(self) -> ComposeResult:
            yield _UnifiedFeed(id="feed")

    async def run_test() -> None:
        app = FeedApp()
        async with app.run_test(size=(60, 16)) as pilot:
            feed = app.query_one("#feed", _UnifiedFeed)
            feed.sync_entries(
                [
                    (1, True, "agente A executando", False),
                    (2, True, "agente B executando", False),
                ]
            )
            await pilot.pause()
            original_slots = list(feed.children)

            feed.sync_entries(
                [
                    (3, False, "resposta final A", False),
                    (4, True, "agente B usando ferramenta", False),
                ]
            )
            await pilot.pause()

            assert list(feed.children) == original_slots
            assert str(original_slots[0].render()) == "resposta final A"
            assert str(original_slots[1].render()) == "agente B usando ferramenta"

            feed.sync_entries(
                [
                    (3, False, "resposta final A", False),
                    (5, False, "resposta final B", False),
                ]
            )
            await pilot.pause()

            assert list(feed.children) == original_slots
            assert [str(slot.render()) for slot in original_slots] == [
                "resposta final A",
                "resposta final B",
            ]

    asyncio.run(run_test())


def test_textual_unified_feed_handles_middle_removal_clear_and_resync():
    import asyncio

    from textual.app import App, ComposeResult

    class FeedApp(App):
        def compose(self) -> ComposeResult:
            yield _UnifiedFeed(id="feed")

    async def run_test() -> None:
        app = FeedApp()
        async with app.run_test(size=(60, 16)) as pilot:
            feed = app.query_one("#feed", _UnifiedFeed)
            feed.sync_entries(
                [
                    (1, False, "mensagem A", False),
                    (2, True, "agente B executando", False),
                    (3, False, "mensagem C", False),
                ]
            )
            await pilot.pause()

            feed.sync_entries(
                [(1, False, "mensagem A", False), (3, False, "mensagem C", False)]
            )
            await pilot.pause()

            assert [str(slot.render()) for slot in feed.children] == [
                "mensagem A",
                "mensagem C",
            ]

            feed.clear_entries()
            await pilot.pause()

            assert list(feed.children) == []
            assert feed._entry_widgets == []
            assert feed._entry_tokens == []

            feed.sync_entries([(4, True, "novo agente executando", False)])
            await pilot.pause()

            assert [str(slot.render()) for slot in feed.children] == [
                "novo agente executando",
            ]

    asyncio.run(run_test())


def test_textual_unified_feed_updates_single_matching_slot_only():
    import asyncio

    from textual.app import App, ComposeResult

    class FeedApp(App):
        def compose(self) -> ComposeResult:
            yield _UnifiedFeed(id="feed")

    async def run_test() -> None:
        app = FeedApp()
        async with app.run_test(size=(60, 16)) as pilot:
            feed = app.query_one("#feed", _UnifiedFeed)
            feed.sync_entries(
                [(1, True, "executando", False), (2, False, "fixo", False)]
            )
            await pilot.pause()

            assert feed.update_entry(0, 1, "pulso atualizado") is True
            assert feed.update_entry(1, 99, "nao deve entrar") is False
            assert feed.update_entry(3, 1, "nao deve entrar") is False

            assert [str(slot.render()) for slot in feed.children] == [
                "pulso atualizado",
                "fixo",
            ]

    asyncio.run(run_test())


def test_toolbar_coordinator_formats_agent_names_with_profile_icons():
    from types import SimpleNamespace

    from quimera.app.agent_pool import AgentPool
    from quimera.app.runtime_state import AppRuntimeState
    from quimera.app.toolbar import ToolbarManager
    from quimera.app.toolbar_coordinator import ToolbarCoordinator

    runtime_state = AppRuntimeState()
    coordinator = ToolbarCoordinator(
        toolbar_manager=ToolbarManager(threads=2),
        agent_pool=AgentPool(["claude"]),
        get_agent_profile=lambda name: SimpleNamespace(name=name, icon="🔮") if name == "claude" else None,
        workspace=SimpleNamespace(cwd=".", branch="main-ui"),
        get_history=lambda: [],
        storage=SimpleNamespace(session_id="s1"),
        bug_store=None,
        get_session_started_at=lambda: None,
        renderer=SimpleNamespace(theme_name="chat"),
        config=None,
        runtime_state=runtime_state,
        input_gate=None,
        get_execution_mode=lambda: SimpleNamespace(name="default"),
        threads=2,
    )
    coordinator.set_parallel_toolbar_state(active_agents=["claude"])

    context = coordinator.build_input_toolbar_context()

    assert context["responder"] == "🔮 Claude"
    assert context["active_agents"] == "🔮 Claude"


def test_textual_toolbar_info_bar_uses_distinct_background():
    from quimera.ui.textual.styles import TEXTUAL_APP_CSS

    css = TEXTUAL_APP_CSS

    assert "#toolbar" in css
    assert "background: #1a1a1a;" in css


def test_textual_toolbar_renderable_uses_main_tui_chip_styles():
    from rich.cells import cell_len
    from rich.text import Text

    gate = TextualInputGate(
        TextualUiBridge(),
        toolbar_context_resolver=lambda: {
            "responder": "🔮 Claude",
            "model": "sonnet",
            "branch": "main-ui",
            "turns": "13",
            "theme": "chat",
            "session": "sessao-2026-07-07-192854",
        },
    )

    renderable = gate._build_toolbar_renderable(max_width=72)

    assert isinstance(renderable, Text)
    plain = renderable.plain
    assert cell_len(plain) <= 72
    assert "🔮 Claude" in plain
    assert "sonnet" in plain
    assert "⎇ main-ui" in plain
    assert "↺ 13" in plain
    assert "✨ chat" in plain
    assert "🔗 " in plain
    assert "sessao-" in plain
    assert "…" in plain


def test_textual_toolbar_uses_full_session_when_width_allows():
    gate = TextualInputGate(
        TextualUiBridge(),
        toolbar_context_resolver=lambda: {
            "responder": "🔮 Claude",
            "model": "sonnet",
            "branch": "main-ui",
            "turns": "13",
            "theme": "chat",
            "session": "sessao-2026-07-07-192854",
        },
    )

    plain = gate._build_toolbar_renderable(max_width=120).plain

    assert "🔗 sessao-2026-07-07-192854" in plain


def test_textual_theme_cycle_bindings_include_main_tui_fallbacks():
    import inspect

    
    source = inspect.getsource(run_textual_quimera_app)

    assert '"ctrl+t", "cycle_theme"' in source
    assert '"alt+t", "cycle_theme"' in source
    assert '"f6", "cycle_theme"' in source


def test_external_textual_window_does_not_reset_after_successful_driver_resume():
    
    events = []

    class FakeDriver:
        can_suspend = True

        def suspend_application_mode(self):
            events.append("driver_suspend")

        def resume_application_mode(self):
            events.append("driver_resume")

    class FakeTextualApp:
        _driver = FakeDriver()

        def call_from_thread(self, callback):
            callback()

        def _suspend_signal(self):
            events.append("suspend_signal")

        def _resume_signal(self):
            events.append("resume_signal")

        def refresh(self, layout=False):
            events.append(f"refresh:{layout}")

        def query_one(self, selector):
            raise LookupError(selector)

    with patch("quimera.ui.textual.terminal_modes._restore_terminal_modes", lambda: events.append("reset")):
        with _external_textual_window(FakeTextualApp()):
            events.append("editor")

    assert events == [
        "suspend_signal",
        "driver_suspend",
        "reset",
        "editor",
        "reset",
        "driver_resume",
        "resume_signal",
        "refresh:True",
    ]


def test_external_textual_window_swaps_stopped_writer_to_avoid_deadlock():
    """Repaints do loop durante o editor não podem travar no writer parado.

    Ao suspender, o writer real do Textual é parado (fila limitada). Se o loop
    continuar emitindo frames, as escritas encheriam a fila e ``put`` bloquearia
    o event loop, impedindo a retomada. O driver deve ficar com um sink
    não-bloqueante durante o processo externo.
    """

    class StoppedWriter:
        """Simula o WriterThread já parado: bloqueia após poucos writes."""

        def __init__(self):
            self.capacity = 3
            self.count = 0

        def write(self, data):
            self.count += 1
            if self.count > self.capacity:
                raise AssertionError("write bloquearia: fila do writer parado cheia")

        def flush(self):
            return None

    class FakeDriver:
        can_suspend = True

        def __init__(self):
            self._writer_thread = StoppedWriter()

        def suspend_application_mode(self):
            # O writer real é parado aqui (fila limitada permanece).
            self._writer_thread.count = self._writer_thread.capacity

        def resume_application_mode(self):
            # start_application_mode() cria um writer novo.
            self._writer_thread = StoppedWriter()

    driver = FakeDriver()

    class FakeTextualApp:
        _driver = driver

        def call_from_thread(self, callback):
            callback()

        def _suspend_signal(self):
            pass

        def _resume_signal(self):
            pass

        def refresh(self, layout=False):
            pass

        def query_one(self, selector):
            raise LookupError(selector)

    with patch("quimera.ui.textual.terminal_modes._restore_terminal_modes", lambda: None):
        with _external_textual_window(FakeTextualApp()):
            # Muito mais escritas do que a capacidade do writer parado: sem o
            # sink de descarte, a 4ª escrita levantaria AssertionError.
            for _ in range(50):
                driver._writer_thread.write("frame")

    # Após a retomada o driver volta a ter um writer real (não o sink).
    assert isinstance(driver._writer_thread, StoppedWriter)


def test_textual_bridge_routes_inline_prompt_answers_to_input_queue_even_with_active_agent():
    bridge = TextualUiBridge()

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            self.writes.append("flush")

    stdin = FakeStdin()
    bridge.attach_quimera_app(
        SimpleNamespace(
            is_agent_running=True,
            active_agent_stdin=stdin,
        )
    )

    bridge.begin_direct_input()
    try:
        bridge.submit_input("cli")
    finally:
        bridge.end_direct_input()

    assert bridge.direct_input_queue.get_nowait() == "cli"
    assert stdin.writes == []


def test_textual_bridge_question_event_routes_approval_answer_to_queue_even_with_active_agent():
    bridge = TextualUiBridge()

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            self.writes.append("flush")

    stdin = FakeStdin()
    bridge.attach_quimera_app(
        SimpleNamespace(
            is_agent_running=True,
            active_agent_stdin=stdin,
        )
    )

    bridge.begin_direct_input()
    bridge.emit(TextualUiEvent("question", {"kind": "approval", "question": "Aprovar?"}))
    bridge.submit_input("y")

    assert bridge.direct_input_queue.get_nowait() == "y"
    assert stdin.writes == []

    bridge.end_direct_input()
    assert bridge.is_direct_input_active() is False


def test_textual_bridge_prompt_clear_does_not_disarm_visible_approval():
    bridge = TextualUiBridge()

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            self.writes.append("flush")

    stdin = FakeStdin()
    bridge.attach_quimera_app(
        SimpleNamespace(
            is_agent_running=True,
            active_agent_stdin=stdin,
        )
    )

    bridge.begin_direct_input()
    bridge.emit(TextualUiEvent("question", {"kind": "approval", "question": "Aprovar?"}))
    bridge.emit(TextualUiEvent("prompt_clear"))
    bridge.submit_input("y")

    assert bridge.direct_input_queue.get_nowait() == "y"
    assert stdin.writes == []

    bridge.end_direct_input()
    assert bridge.is_direct_input_active() is False


def test_textual_bridge_pending_input_routes_approval_answer_to_queue_even_with_active_agent():
    bridge = TextualUiBridge()

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            self.writes.append("flush")

    stdin = FakeStdin()
    bridge.attach_quimera_app(
        SimpleNamespace(
            is_agent_running=True,
            active_agent_stdin=stdin,
        )
    )

    bridge.begin_direct_input()
    bridge.emit(TextualUiEvent("pending_input", {"kind": "approval", "question": "Aprovar?"}, agent="local"))
    bridge.submit_input("y")

    assert bridge.direct_input_queue.get_nowait() == "y"
    assert stdin.writes == []

    bridge.end_direct_input()
    assert bridge.is_direct_input_active() is False


def test_textual_bridge_approval_answer_cannot_be_consumed_by_normal_input_queue():
    bridge = TextualUiBridge()

    bridge.begin_direct_input()
    bridge.emit(TextualUiEvent("question", {"kind": "approval", "question": "Aprovar?"}))
    bridge.submit_input("a")

    assert bridge.input_queue.empty()
    assert bridge.direct_input_queue.get_nowait() == "a"
    bridge.end_direct_input()


def test_textual_input_gate_marks_inline_connection_prompts_as_direct_input():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)
    emitted = []
    bridge.emit = emitted.append

    assert bridge.is_direct_input_active() is False

    bridge.input_queue.put("cmd")
    result = gate("Tipo de conexão")

    assert result == "cmd"
    assert bridge.is_direct_input_active() is False
    assert [event.kind for event in emitted].count("prompt") == 1
    assert emitted[-1].kind == "prompt_clear"


def test_textual_input_gate_clear_interactive_prompt_state_resets_toolbar_mode():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)

    gate._interactive_prompt_active = True
    assert gate._build_toolbar_text() == "Enter: confirmar  |  Ctrl+C: cancelar"

    gate.clear_interactive_prompt_state()

    assert gate._build_toolbar_text() == ""


def test_textual_input_gate_arms_direct_input_before_approval_question_event():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)
    direct_state_at_question = []
    emitted = []

    def capture(event):
        emitted.append(event)
        if event.kind == "question":
            direct_state_at_question.append(bridge.is_direct_input_active())

    bridge.emit = capture
    bridge.direct_input_queue.put("y")

    result = gate.read_approval_in_terminal("Aprovar shell?", "Executar? ")

    assert result == "y"
    assert direct_state_at_question == [True]
    assert bridge.is_direct_input_active() is False
    assert [event.kind for event in emitted][-2:] == ["question_clear", "prompt_clear"]


def test_textual_renderer_commit_agent_stream_materializes_active_stream():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.start_message_stream("claude")
    renderer.update_message_stream("claude", "linha 1\n")
    renderer.update_message_stream("claude", {"text": "linha 2"})

    assert renderer.commit_agent_stream("claude", render_mode="plain") is True

    assert emitted[-1].kind == "agent_message"
    assert emitted[-1].agent == "claude"
    assert emitted[-1].payload["content"] == "linha 1\nlinha 2"


def test_textual_renderer_commit_agent_stream_returns_false_without_content():
    renderer = TextualRenderer(TextualUiBridge())

    renderer.start_message_stream("claude")

    assert renderer.commit_agent_stream("claude") is False


def test_textual_direct_input_submission_clears_approval_overlay_before_queueing_answer():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append

    bridge.begin_direct_input()
    try:
        bridge.submit_input("a")
    finally:
        bridge.end_direct_input()

    assert emitted[-1].kind == "question_clear"
    assert bridge.direct_input_queue.get_nowait() == "a"


def test_textual_input_window_without_question_does_not_leave_visual_overlay_active():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    with renderer.input_window(owner="claude"):
        pass

    assert emitted == []
    assert bridge.is_direct_input_active() is False


def test_textual_bridge_handler_refreshes_after_visual_event_updates():
    import inspect

    
    source = inspect.getsource(run_textual_quimera_app)

    assert "def _refresh_now" in source
    assert "self._refresh_now(layout=True)" in source
    assert "self._refresh_now()" in source
    assert "_logger.exception(\"Falha ao atualizar a interface Textual\")" in source


def test_textual_app_syncs_last_expired_tool_before_early_return():
    import inspect

    source = inspect.getsource(run_textual_quimera_app)
    pulse_start = source.index("def _pulse_thinking_marker")
    pulse_end = source.index("def _run_quimera_app", pulse_start)
    pulse_source = source[pulse_start:pulse_end]

    expiry_branch = pulse_source.index("if expired_tools:")
    empty_branch = pulse_source.index("if not self._feed_model.has_transients")
    assert expiry_branch < empty_branch
    assert "self._sync_feed()" in pulse_source[expiry_branch:empty_branch]
    assert "self._refresh_now()" in pulse_source[expiry_branch:empty_branch]
    assert "any(item.transient" not in pulse_source
    assert "self._sync_transient_feed_slots()" in pulse_source


def test_textual_unified_feed_uses_actual_scroll_position_for_auto_follow():
    import inspect

    source = inspect.getsource(run_textual_quimera_app)
    sync_start = source.index("def _sync_feed")
    sync_end = source.index("def _redraw_feed", sync_start)
    sync_source = source[sync_start:sync_end]

    assert "_feed_pinned_to_bottom" not in source
    assert "was_pinned = feed.is_vertical_scroll_end" in sync_source
    assert "if renderable is not None:" in sync_source


def test_textual_prompt_submission_scrolls_feed_to_end_once():
    import inspect

    source = inspect.getsource(run_textual_quimera_app)

    submit_start = source.index("def on_input_submitted")
    submit_end = source.index("def _set_question_overlay", submit_start)
    submit_source = source[submit_start:submit_end]
    # Enviar o prompt leva o scroll ao fim imediatamente e agenda o pin
    # para quando o turno do usuário entrar no feed.
    assert "self._scroll_feed_on_submit = True" in submit_source
    assert "feed.scroll_end" in submit_source

    sync_start = source.index("def _sync_feed")
    sync_end = source.index("def _sync_transient_feed_slots", sync_start)
    sync_source = source[sync_start:sync_end]
    # A flag é consumida uma única vez: depois o auto-follow volta a
    # depender do usuário estar no fim (was_pinned).
    assert "self._scroll_feed_on_submit = False" in sync_source
    assert sync_source.index("self._scroll_feed_on_submit = False") < sync_source.index(
        "feed.sync_entries(entries, force=force)"
    )


def test_textual_app_pulse_updates_only_transient_slots():
    import inspect

    source = inspect.getsource(run_textual_quimera_app)
    transient_start = source.index("def _sync_transient_feed_slots")
    transient_end = source.index("def _redraw_feed", transient_start)
    transient_source = source[transient_start:transient_end]

    assert "self._feed_model.transient_items()" in transient_source
    assert "feed.update_entry(index, token, renderable)" in transient_source
    assert "self._sync_feed()" in transient_source



def test_textual_app_periodically_drains_bridge_event_queue():
    import inspect

    
    source = inspect.getsource(run_textual_quimera_app)

    assert "self.set_interval(0.05, self._drain_bridge_events)" in source
    assert "def _drain_bridge_events" in source
    assert "bridge.drain_pending_events()" in source


def test_textual_renderer_flush_drains_bridge_events_for_tool_previews():
    bridge = TextualUiBridge()
    calls = []

    class FakeTextualApp:
        def handle_bridge_event(self, event):
            calls.append(event.kind)

        def flush_bridge_events(self):
            calls.append("flush")

        def call_from_thread(self, callback, *args):
            callback(*args)

    bridge.attach_textual_app(FakeTextualApp())
    renderer = TextualRenderer(bridge)

    renderer.show_system_neutral("tool: list_files")
    assert renderer.flush_quick() is True
    renderer.flush()

    assert calls == ["muted", "flush", "flush"]


def test_textual_app_exposes_flush_bridge_events_for_immediate_tool_preview_rendering():
    import inspect

    
    source = inspect.getsource(run_textual_quimera_app)

    assert "def flush_bridge_events" in source
    assert "self._drain_bridge_events()" in source
    assert "self._refresh_now(layout=True)" in source


def test_textual_app_status_bar_tracks_tool_preview_events():
    import inspect

    source = inspect.getsource(run_textual_quimera_app)

    assert "_active_tool_previews" in source
    assert 'event.kind == "tool_preview"' in source
    assert "def _update_status_bar" in source
    assert 'self.query_one("#status_bar", Static)' in source
    assert 'text.append("[spy]"' not in source
    assert 'text.append("processando...")' not in source


def test_textual_app_uses_question_overlay_for_prompt_routing():
    import inspect

    
    source = inspect.getsource(run_textual_quimera_app)

    assert "def _set_question_overlay" in source
    assert "def _clear_question_overlay" in source
    assert "self._clear_prompt_state()" in source
    assert "clear_interactive_prompt_state" in source


def test_textual_renderer_emits_pending_input_card_event():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    renderer = TextualRenderer(bridge)

    renderer.set_agent_pending_input("claude", "approval", "Executar comando?\npytest")

    assert emitted[-1].kind == "pending_input"
    assert emitted[-1].agent == "claude"
    assert emitted[-1].payload["kind"] == "approval"
    assert emitted[-1].payload["question"] == "Executar comando?\npytest"


def test_textual_feed_treats_pending_input_as_transient_agent_state():
    model = TextualFeedModel()
    pending = TextualUiEvent(
        "pending_input",
        {"label": "Claude", "kind": "input", "question": "Responder?"},
        agent="claude",
    )
    final = TextualUiEvent("agent_message", {"content": "feito", "label": "Claude"}, agent="claude")

    assert model.apply(pending) is True
    assert model.items[-1].transient is True
    assert model.apply(final) is True

    assert len(model.items) == 1
    assert model.items[0].event is final


def test_textual_normal_chat_input_goes_to_main_input_queue():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)
    emitted = []
    bridge.emit = emitted.append

    assert not bridge.is_direct_input_active()

    bridge.input_queue.put("mensagem normal")
    result = gate("mensagem...")

    assert result == "mensagem normal"
    assert bridge.direct_input_queue.empty()
    assert bridge.input_queue.empty()


def test_textual_modal_question_input_goes_to_direct_input_queue():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append

    bridge.begin_direct_input()
    bridge.direct_input_queue.put("resposta modal")
    result = bridge.direct_input_queue.get(timeout=1)

    assert result == "resposta modal"
    assert bridge.input_queue.empty()
    bridge.end_direct_input()


def test_textual_approval_answer_does_not_enter_main_input_queue():
    bridge = TextualUiBridge()

    bridge.begin_direct_input()
    bridge.emit(TextualUiEvent("question", {"kind": "approval", "question": "Aprovar?"}))
    bridge.submit_input("y")

    assert bridge.input_queue.empty()
    assert bridge.direct_input_queue.get_nowait() == "y"
    bridge.end_direct_input()


def test_textual_chat_prompt_active_does_not_steal_normal_input():
    bridge = TextualUiBridge()
    gate = TextualInputGate(bridge)
    emitted = []
    bridge.emit = emitted.append

    assert not bridge.is_direct_input_active()

    bridge.input_queue.put("comando para agente")
    result = gate("mensagem...")

    assert result == "comando para agente"
    assert bridge.direct_input_queue.empty()
    assert bridge.input_queue.empty()
    assert not bridge.is_direct_input_active()


def test_textual_app_imports_summary_spinner_used_by_spinner_update():
    import inspect

    from quimera.ui.textual.app import run_textual_quimera_app

    source = inspect.getsource(run_textual_quimera_app)

    assert "_SummarySpinner" in source
    assert "from quimera.ui.textual.widgets import" in source


def test_textual_app_routes_notification_events_to_notify():
    import inspect

    from quimera.ui.textual.app import run_textual_quimera_app

    source = inspect.getsource(run_textual_quimera_app)

    assert 'event.kind == "notification"' in source
    assert "self.notify(" in source
    assert 'event.kind == "summarizing"' in source
