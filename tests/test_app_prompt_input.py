"""Tests for Textual UI input gate and renderer modules."""
import asyncio
import threading
import time

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_textual_input_gate_reads_submitted_line():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    gate = bridge.create_input_gate(command_resolver=lambda: ["/help"])
    result = []

    thread = threading.Thread(target=lambda: result.append(gate("Alex: ")))
    thread.start()

    bridge.submit_input("oi")
    thread.join(timeout=1)

    assert result == ["oi"]
    assert gate.is_active() is False


def test_textual_renderer_buffers_events_until_app_attaches():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    renderer = bridge.create_renderer()
    renderer.show_system("iniciando")
    renderer.show_message("codex", "resposta")

    events = bridge.drain_pending_events()

    assert [event.kind for event in events] == ["system", "agent_message"]
    assert events[1].agent == "codex"


def test_textual_renderer_extracts_text_from_rich_renderable():
    from rich.panel import Panel
    from rich.text import Text

    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    renderer = bridge.create_renderer()
    renderer.show_message("codex", Panel(Text("conteudo interno"), title="Titulo"))

    events = bridge.drain_pending_events()

    assert "conteudo interno" in events[0].payload["content"]
    assert "rich.panel.Panel object" not in events[0].payload["content"]


def test_textual_renderer_strips_ansi_from_agent_message():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    renderer = bridge.create_renderer()
    renderer.show_message("codex", "\x1b[31mconteudo\x1b[0m")

    events = bridge.drain_pending_events()

    assert events[0].payload["content"] == "conteudo"


def test_textual_input_gate_redisplay_sends_prompt_event():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    gate = bridge.create_input_gate(command_resolver=lambda: ["/help"])

    events = []
    original_emit = bridge.emit
    bridge.emit = lambda e: events.append(e.kind)

    gate._set_active_state(True)
    gate.redisplay()
    gate._set_active_state(False)

    assert "prompt" in events


def test_textual_input_gate_completions():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    gate = bridge.create_input_gate(
        command_resolver=lambda: ["/help", "/exit"],
        argument_resolver=lambda cmd, p: ["branch1", "branch2"] if cmd == "/context" else [],
    )

    assert "/help" in gate.completions_for("/h")
    assert "/exit" in gate.completions_for("/e")
    assert "/context branch1" in gate.completions_for("/context b")


def test_textual_input_gate_get_line_buffer_returns_empty():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    gate = bridge.create_input_gate()

    assert gate.get_line_buffer() == ""


def test_textual_renderer_show_no_response():
    from quimera.ui.textual.bridge import TextualUiBridge

    bridge = TextualUiBridge()
    renderer = bridge.create_renderer()
    renderer.show_no_response("codex")

    events = bridge.drain_pending_events()
    assert events[0].kind == "agent_message"
    assert "sem resposta" in str(events[0].payload)


def test_textual_feed_limit_ignores_auto_summarize_threshold():
    from quimera.ui.textual.app import _resolve_textual_feed_limit

    app = SimpleNamespace(
        auto_summarize_threshold=5,
        prompt_builder=SimpleNamespace(history_window=12),
    )

    assert _resolve_textual_feed_limit(app) is None


def test_textual_feed_limit_ignores_history_window():
    from quimera.ui.textual.app import _resolve_textual_feed_limit

    app = SimpleNamespace(
        auto_summarize_threshold=0,
        prompt_builder=SimpleNamespace(history_window=12),
    )

    assert _resolve_textual_feed_limit(app) is None


def test_textual_rich_log_max_lines_prunes_visible_feed():
    from textual.app import App, ComposeResult
    from textual.widgets import RichLog

    class FeedApp(App):
        def compose(self) -> ComposeResult:
            yield RichLog(id="feed", max_lines=3, wrap=True)

    async def run_test() -> None:
        app = FeedApp()
        async with app.run_test() as pilot:
            feed = app.query_one("#feed", RichLog)
            for line in ("a", "b", "c", "d"):
                feed.write(line)
            await pilot.pause()
            assert feed.max_lines == 3
            assert len(feed.lines) <= 3

    asyncio.run(run_test())


def test_textual_summary_spinner_uses_circular_frames():
    from quimera.ui.textual.constants import SUMMARY_SPINNER_FRAMES as _SUMMARY_SPINNER_FRAMES

    assert _SUMMARY_SPINNER_FRAMES == ("◐", "◓", "◑", "◒")


def test_textual_post_exit_failure_recorder_keeps_errors_and_warnings():
    from quimera.ui.textual.events import TextualUiEvent
    from quimera.ui.textual.app import _append_post_exit_failure_message

    messages = []

    assert _append_post_exit_failure_message(messages, TextualUiEvent("error", "falha")) is True
    assert _append_post_exit_failure_message(messages, TextualUiEvent("warning", "atenção")) is True
    assert _append_post_exit_failure_message(messages, TextualUiEvent("plain", "ignorar")) is False

    assert messages == [("error", "falha"), ("warning", "atenção")]


def test_textual_post_exit_failure_recorder_ignores_empty_payload():
    from quimera.ui.textual.events import TextualUiEvent
    from quimera.ui.textual.app import _append_post_exit_failure_message

    messages = []

    assert _append_post_exit_failure_message(messages, TextualUiEvent("error", "")) is False
    assert _append_post_exit_failure_message(messages, TextualUiEvent("warning", None)) is False

    assert messages == []


def test_completion_input_pastes_clipboard_payload():
    from textual.app import App, ComposeResult

    from quimera.ui.textual.widgets import _CompletionInput

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input", clipboard_paste_handler=lambda: "texto colado")

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.cursor_position = len(widget.value)
            widget.action_paste_clipboard()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert widget.user_value == "texto colado"

    asyncio.run(run_test())


def test_completion_input_displays_attachment_placeholder_and_submits_marker():
    from textual.app import App, ComposeResult

    from quimera.clipboard_support import ClipboardManager
    from quimera.ui.textual.widgets import _CompletionInput

    marker = ClipboardManager().marker_for("/tmp/quimera-clipboard-test.png")

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input", clipboard_paste_handler=lambda: marker)

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.cursor_position = len(widget.value)
            widget.action_paste_clipboard()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert widget.user_value == "🖼 imagem anexada 1"
            assert widget.submission_value == marker

    asyncio.run(run_test())


def test_completion_input_has_f8_clipboard_fallback_binding():
    from quimera.ui.textual.widgets import _CompletionInput

    bindings = {
        binding.key: binding.action
        for binding in _CompletionInput.BINDINGS
    }

    assert bindings["ctrl+v"] == "paste_clipboard"
    assert bindings["f8"] == "paste_clipboard"


def test_completion_input_notifies_when_clipboard_paste_is_empty():
    from textual.app import App, ComposeResult

    from quimera.ui.textual.widgets import _CompletionInput

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input", clipboard_paste_handler=lambda: None)

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.action_paste_clipboard()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert widget.user_value == ""
            assert any(
                "clipboard" in notification.title.lower()
                for notification in app._notifications
            )

    asyncio.run(run_test())


def test_completion_input_insert_user_text_preserves_prefix():
    from textual.app import App, ComposeResult

    from quimera.ui.textual.widgets import _CompletionInput

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input")

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.cursor_position = 0
            widget.insert_user_text("abc")
            await pilot.pause()
            assert widget.value.startswith(">>>: ")
            assert widget.user_value == "abc"

    asyncio.run(run_test())


def test_completion_input_history_navigation_preserves_fixed_prefix():
    from textual.app import App, ComposeResult

    from quimera.app.completion_dropdown import CompletionDropdown
    from quimera.ui.textual.widgets import _CompletionInput

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input")
            yield CompletionDropdown()

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.set_prefix(">>> ")
            widget.add_to_history("primeiro")
            widget.add_to_history("segundo")
            widget.value = "rascunho sem prefixo"
            await pilot.pause()

            await pilot.press("up")
            await pilot.pause()
            assert widget.value == ">>> segundo"
            assert widget.user_value == "segundo"

            await pilot.press("down")
            await pilot.pause()
            assert widget.value == ">>> rascunho sem prefixo"
            assert widget.user_value == "rascunho sem prefixo"

    asyncio.run(run_test())


def test_completion_input_history_strips_legacy_prompt_prefix(tmp_path):
    from textual.app import App, ComposeResult

    from quimera.app.completion_dropdown import CompletionDropdown
    from quimera.ui.textual.widgets import _CompletionInput

    history_file = tmp_path / "history.jsonl"
    history_file.write_text('">>> antigo json"\n>>>: antigo texto\n', encoding="utf-8")

    class InputApp(App):
        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input")
            yield CompletionDropdown()

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.set_prefix(">>> ")
            widget.load_history(history_file)

            await pilot.press("up")
            await pilot.pause()
            assert widget.value == ">>> antigo texto"

            await pilot.press("up")
            await pilot.pause()
            assert widget.value == ">>> antigo json"

    asyncio.run(run_test())


def test_completion_input_submit_waits_for_pending_clipboard_paste():
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    from quimera.app.completion_dropdown import CompletionDropdown
    from quimera.ui.textual.widgets import _CompletionInput

    class InputApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.submitted_values: list[str] = []

        def compose(self) -> ComposeResult:
            yield _CompletionInput(
                id="input",
                clipboard_paste_handler=lambda: (time.sleep(0.05), "imagem colada")[1],
            )
            yield CompletionDropdown()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.submitted_values.append(event.input.user_value)

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.action_paste_clipboard()
            await widget.action_submit()
            await pilot.pause()
            assert app.submitted_values == ["imagem colada"]

    asyncio.run(run_test())


def test_completion_input_submit_expands_attachment_placeholder():
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    from quimera.app.completion_dropdown import CompletionDropdown
    from quimera.clipboard_support import ClipboardManager
    from quimera.ui.textual.widgets import _CompletionInput

    marker = ClipboardManager().marker_for("/tmp/quimera-clipboard-submit.png")

    class InputApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.submitted_values: list[str] = []

        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input", clipboard_paste_handler=lambda: marker)
            yield CompletionDropdown()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            value = getattr(event.input, "submission_value", event.input.user_value)
            self.submitted_values.append(value)

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.action_paste_clipboard()
            await widget.action_submit()
            await pilot.pause()
            assert widget.user_value == "🖼 imagem anexada 1"
            assert app.submitted_values == [marker]

    asyncio.run(run_test())


def test_completion_input_submit_waits_for_second_clipboard_paste_after_cancel():
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    from quimera.app.completion_dropdown import CompletionDropdown
    from quimera.ui.textual.widgets import _CompletionInput

    class InputApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.submitted_values: list[str] = []
            self._paste_count = 0

        def _paste(self) -> str:
            self._paste_count += 1
            if self._paste_count == 1:
                time.sleep(0.2)
                return "primeiro paste"
            time.sleep(0.05)
            return "segundo paste"

        def compose(self) -> ComposeResult:
            yield _CompletionInput(id="input", clipboard_paste_handler=self._paste)
            yield CompletionDropdown()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.submitted_values.append(event.input.user_value)

    async def run_test() -> None:
        app = InputApp()
        async with app.run_test() as pilot:
            widget = app.query_one("#input", _CompletionInput)
            widget.action_paste_clipboard()
            await pilot.pause(0.01)
            widget.action_paste_clipboard()
            await widget.action_submit()
            await pilot.pause()
            assert app.submitted_values == ["segundo paste"]

    asyncio.run(run_test())


def test_simple_input_gate_basic():
    from quimera.app.simple_input_gate import SimpleInputGate

    gate = SimpleInputGate()
    assert gate.is_active() is False
    assert gate.get_owner_thread_id() is None
    assert gate.get_line_buffer() == ""
    assert gate.run_in_terminal_message(lambda: None) is False


def test_simple_input_gate_setters():
    from quimera.app.simple_input_gate import SimpleInputGate

    gate = SimpleInputGate()
    handler = lambda: None
    gate.set_theme_cycle_handler(handler)
    assert gate._theme_cycle_handler is handler

    resolver = lambda: []
    gate.set_command_resolver(resolver)
    assert gate._command_resolver is resolver

    gate.set_toolbar_context_resolver(resolver)
    assert gate._toolbar_context_resolver is resolver

    gate.set_argument_resolver(resolver)
    assert gate._argument_resolver is resolver


def test_simple_input_gate_active_state():
    from quimera.app.simple_input_gate import SimpleInputGate

    gate = SimpleInputGate()
    gate._set_active_state(True)
    assert gate.is_active() is True
    assert gate.get_owner_thread_id() is not None
    gate._set_active_state(False)
    assert gate.is_active() is False
    assert gate.get_owner_thread_id() is None


def test_simple_input_gate_redisplay_does_nothing():
    from quimera.app.simple_input_gate import SimpleInputGate

    gate = SimpleInputGate()
    gate.redisplay()


def test_prompt_formatter():
    from quimera.app.prompt_formatter import PromptFormatter

    assert PromptFormatter.format_user_prompt("Alex", None) == "Alex: "
    assert PromptFormatter.format_user_prompt("Alex", "code") == "Alex [code]: "
    assert PromptFormatter.format_user_prompt("", None) == ">>> "
    assert PromptFormatter.format_user_prompt(">", None) == "> "
    assert PromptFormatter.format_user_prompt(">", "code") == "> [code]: "
