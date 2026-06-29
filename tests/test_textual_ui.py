"""Tests for the Textual UI bridge/feed model."""

from unittest.mock import Mock, patch
from contextlib import contextmanager

from quimera.app.textual_ui import TextualFeedModel, TextualInputGate, TextualRenderer, TextualUiBridge, TextualUiEvent


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
    assert model.items[0].event.payload == "Oi, Alex"

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


def test_textual_input_gate_clears_question_overlay_after_selection_timeout():
    bridge = TextualUiBridge()
    emitted = []
    bridge.emit = emitted.append
    gate = TextualInputGate(bridge)

    result = gate.read_selection_in_terminal("Escolha", ["sim", "não"], timeout=0.001)

    assert result is None
    assert [event.kind for event in emitted] == ["question", "input_active", "prompt", "input_active", "question_clear"]

    gate.set_textual_mounted(True)

    assert gate.is_active() is True

    gate.set_textual_mounted(False)

    assert gate.is_active() is False


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

    class FakeTextualApp:
        @contextmanager
        def suspend(self):
            events.append("suspend")
            yield
            events.append("resume")

    bridge.attach_textual_app(FakeTextualApp())
    renderer = TextualRenderer(bridge)

    with renderer.external_window("external:editor", title="Editor externo"):
        events.append("editor")

    assert events == ["suspend", "editor", "resume"]


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
