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


def test_textual_input_gate_is_active_while_textual_is_mounted():
    gate = TextualInputGate(TextualUiBridge())

    assert gate.is_active() is False

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
