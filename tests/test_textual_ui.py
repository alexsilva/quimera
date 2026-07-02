"""Tests for the Textual UI bridge/feed model."""

from unittest.mock import Mock, patch
from contextlib import contextmanager
from types import SimpleNamespace

from rich.console import Console, Group

from quimera.app.textual_ui import (
    TextualFeedModel,
    TextualInputGate,
    TextualRenderer,
    TextualUiBridge,
    TextualUiEvent,
    _build_question_overlay,
    _build_window_overlay_payload,
    _clear_question_overlay_widget,
    _render_event,
)


def _events(model: TextualFeedModel):
    return [item.event for item in model.items]


def test_textual_feed_replaces_agent_lifecycle_with_final_message():
    model = TextualFeedModel()

    assert model.apply(TextualUiEvent("agent_lifecycle", {"status": "completed", "message": "execução concluída"}, agent="claude"))
    assert len(model.items) == 1
    assert model.items[0].transient is True
    assert model.items[0].event.kind == "agent_lifecycle"

    final = TextualUiEvent("agent_message", {"content": "Oi, Alex!", "label": "Claude"}, agent="claude")
    assert model.apply(final)

    assert len(model.items) == 1
    assert model.items[0].transient is False
    assert model.items[0].event is final
    assert model.last_change.redraw is True


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

    assert len(model.items) == 2
    assert model.items[0].event.kind == "agent_message"
    assert model.items[1].event.kind == "agent_lifecycle"
    assert model.items[1].transient is True


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
    assert event.payload == {"status": "completed", "message": "execução concluída"}


def test_textual_bridge_injects_input_into_active_agent_stdin():
    bridge = TextualUiBridge()
    stdin = Mock()
    app = Mock(is_agent_running=True, active_agent_stdin=stdin)
    bridge.attach_quimera_app(app)

    bridge.submit_input("continua")

    stdin.write.assert_called_once_with("continua\n")
    stdin.flush.assert_called_once()
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
        "s/sim/y/yes = aprovar",
        "n/não/no/enter = negar",
        "a/all/todas = aprovar todas",
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

    with patch("quimera.app.textual_ui.sys.__stdout__", FakeStdout()):
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
    from quimera.app.textual_ui import _render_event

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
    assert "⚠" in str(rendered)
    assert "git_commit :: risco: write" in str(rendered)
    assert "opencode-big-pickle" in str(rendered)


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
    assert "s/sim/y/yes = aprovar" in emitted[0].payload["options"]
    assert _build_window_overlay_payload(emitted[0].payload) == {
        "question": "Executar shell?",
        "options": [
            "s/sim/y/yes = aprovar",
            "n/não/no/enter = negar",
            "a/all/todas = aprovar todas",
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
                "s/sim/y/yes = aprovar",
                "n/não/no/enter = negar",
            ],
        }
    )
    console = Console(width=80, record=True, force_terminal=False)

    console.print(renderable)
    output = console.export_text()

    assert "Permissão solicitada" in output
    assert "Executar comando via shell?" in output
    assert "s/sim/y/yes = aprovar" in output
    assert "n/não/no/enter = negar" in output


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


def test_textual_bridge_does_not_echo_slash_command_as_user_message():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append

    bridge.submit_input("/agents")

    assert bridge.input_queue.get_nowait() == "/agents"
    assert emitted == []


def test_textual_user_message_renders_as_chat_turn():
    rendered = _render_event(TextualUiEvent("user_message", {"content": "oi", "label": "Alex"}))

    assert rendered is not None


def test_textual_feed_reserves_at_least_ten_lines_for_agent_output():
    import inspect

    from quimera.app.textual_ui import run_textual_quimera_app

    css = inspect.getsource(run_textual_quimera_app)

    assert "#main" in css
    assert "min-height: 14;" in css
    assert "#feed" in css
    assert "min-height: 10;" in css
    assert "#question_overlay" in css
    assert "max-height: 12;" in css
    assert "overflow-y: auto;" in css
    assert "#input_bar" in css
    assert "max-height: 3;" in css


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
        get_pending_input_for=lambda: "claude",
        get_execution_mode=lambda: SimpleNamespace(name="default"),
        threads=2,
    )
    coordinator.set_parallel_toolbar_state(active_agents=["claude"])

    context = coordinator.build_input_toolbar_context()

    assert context["responder"] == "🔮 Claude"
    assert context["active_agents"] == "🔮 Claude"


def test_textual_toolbar_info_bar_uses_distinct_background():
    import inspect

    from quimera.app.textual_ui import run_textual_quimera_app

    css = inspect.getsource(run_textual_quimera_app)

    assert "#toolbar" in css
    assert "background: #252526;" in css


def test_textual_toolbar_renderable_uses_main_tui_chip_styles():
    from rich.text import Text

    gate = TextualInputGate(
        TextualUiBridge(),
        toolbar_context_resolver=lambda: {
            "responder": "🔮 Claude",
            "model": "sonnet",
            "branch": "main-ui",
            "turns": "13",
            "theme": "chat",
        },
    )

    renderable = gate._build_toolbar_renderable()

    assert isinstance(renderable, Text)
    plain = renderable.plain
    assert "🔮 Claude" in plain
    assert "sonnet" in plain
    assert "⎇ main-ui" in plain
    assert "↺ 13" in plain
    assert "✨ chat" in plain


def test_textual_theme_cycle_bindings_include_main_tui_fallbacks():
    import inspect

    from quimera.app.textual_ui import run_textual_quimera_app

    source = inspect.getsource(run_textual_quimera_app)

    assert '"ctrl+t", "cycle_theme"' in source
    assert '"alt+t", "cycle_theme"' in source
    assert '"f6", "cycle_theme"' in source


def test_external_textual_window_does_not_reset_after_successful_driver_resume():
    from quimera.app.textual_ui import _external_textual_window

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

    with patch("quimera.app.textual_ui._restore_terminal_modes", lambda: events.append("reset")):
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

    from quimera.app.textual_ui import run_textual_quimera_app

    source = inspect.getsource(run_textual_quimera_app)

    assert "def _refresh_now" in source
    assert "self._refresh_now(layout=True)" in source
    assert "self._refresh_now()" in source



def test_textual_app_periodically_drains_bridge_event_queue():
    import inspect

    from quimera.app.textual_ui import run_textual_quimera_app

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

    from quimera.app.textual_ui import run_textual_quimera_app

    source = inspect.getsource(run_textual_quimera_app)

    assert "def flush_bridge_events" in source
    assert "self._drain_bridge_events()" in source
    assert "self._refresh_now(layout=True)" in source


def test_textual_app_uses_question_overlay_for_prompt_routing():
    import inspect

    from quimera.app.textual_ui import run_textual_quimera_app

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
