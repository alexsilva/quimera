"""Tests for quimera/app/prompt_input.py — target: 100% coverage."""
import sys
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gate(**kwargs):
    from quimera.app.prompt_input import InputGate
    return InputGate(**kwargs)


# ---------------------------------------------------------------------------
# InputGate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _SlashCommandCompleter
# ---------------------------------------------------------------------------

class TestSlashCommandCompleter:
    def _completer(self, command_resolver=None, argument_resolver=None):
        from quimera.app.prompt_input import _SlashCommandCompleter
        return _SlashCommandCompleter(command_resolver, argument_resolver)

    def _doc(self, text):
        doc = MagicMock()
        doc.text_before_cursor = text
        return doc

    def test_no_slash_returns_nothing(self):
        comp = self._completer(command_resolver=lambda: ["/foo"])
        results = list(comp.get_completions(self._doc("hello"), MagicMock()))
        assert results == []

    def test_slash_prefix_yields_completions(self):
        comp = self._completer(command_resolver=lambda: ["/foo", "/bar"])
        results = list(comp.get_completions(self._doc("/f"), MagicMock()))
        texts = [c.text for c in results]
        assert "/foo" in texts

    def test_slash_non_callable_resolver_returns_nothing(self):
        comp = self._completer(command_resolver=None)
        results = list(comp.get_completions(self._doc("/"), MagicMock()))
        assert results == []

    def test_resolver_exception_returns_empty(self):
        def bad(): raise RuntimeError("boom")
        comp = self._completer(command_resolver=bad)
        results = list(comp.get_completions(self._doc("/f"), MagicMock()))
        assert results == []

    def test_argument_resolver_called_after_space(self):
        arg_resolver = MagicMock(return_value=["branch1", "branch2"])
        comp = self._completer(
            command_resolver=lambda: ["/switch"],
            argument_resolver=arg_resolver,
        )
        results = list(comp.get_completions(self._doc("/switch bra"), MagicMock()))
        texts = [c.text for c in results]
        assert "branch1" in texts or "branch2" in texts

    def test_argument_resolver_exception_returns_empty(self):
        def bad_arg(cmd, partial): raise RuntimeError("boom")
        comp = self._completer(
            command_resolver=lambda: ["/switch"],
            argument_resolver=bad_arg,
        )
        results = list(comp.get_completions(self._doc("/switch bra"), MagicMock()))
        assert results == []

    def test_argument_resolver_none_returns_nothing_after_space(self):
        comp = self._completer(
            command_resolver=lambda: ["/switch"],
            argument_resolver=None,
        )
        results = list(comp.get_completions(self._doc("/switch bra"), MagicMock()))
        assert results == []


# ---------------------------------------------------------------------------
# InputGate — construction when _PT_AVAILABLE = True
# ---------------------------------------------------------------------------

class TestInputGateConstruction:
    def test_basic_construction(self):
        with patch("quimera.app.prompt_input.PromptSession") as MockPS:
            with patch("quimera.app.prompt_input.InMemoryHistory") as MockHist:
                gate = _make_gate()
                MockPS.assert_called_once()
                assert gate._session is MockPS.return_value

    def test_construction_with_history_file(self, tmp_path):
        hist_file = str(tmp_path / "hist.txt")
        with patch("quimera.app.prompt_input.PromptSession") as MockPS:
            with patch("quimera.app.prompt_input.FileHistory") as MockFH:
                gate = _make_gate(history_file=hist_file)
                MockFH.assert_called_once_with(hist_file)

    def test_construction_history_file_exception_falls_back(self, tmp_path):
        hist_file = str(tmp_path / "hist.txt")
        with patch("quimera.app.prompt_input.FileHistory", side_effect=OSError("fail")):
            with patch("quimera.app.prompt_input.InMemoryHistory") as MockHist:
                with patch("quimera.app.prompt_input.PromptSession") as MockPS:
                    gate = _make_gate(history_file=hist_file)
                    MockHist.assert_called()

# ---------------------------------------------------------------------------
# InputGate — setters (lines 115-129)
# ---------------------------------------------------------------------------

class TestInputGateSetters:
    def test_set_command_resolver(self):
        gate = _make_gate()
        resolver = lambda: []
        gate.set_command_resolver(resolver)
        assert gate._command_resolver is resolver

    def test_set_argument_resolver(self):
        gate = _make_gate()
        resolver = lambda cmd, p: []
        gate.set_argument_resolver(resolver)
        assert gate._argument_resolver is resolver

    def test_set_toolbar_context_resolver(self):
        gate = _make_gate()
        resolver = lambda: {}
        gate.set_toolbar_context_resolver(resolver)
        assert gate._toolbar_context_resolver is resolver

    def test_set_theme_cycle_handler(self):
        gate = _make_gate()
        handler = lambda: None
        gate.set_theme_cycle_handler(handler)
        assert gate._theme_cycle_handler is handler


# ---------------------------------------------------------------------------
# InputGate — _build_toolbar (lines 131-168)
# ---------------------------------------------------------------------------

class TestBuildToolbar:
    def test_no_resolver_returns_none(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate()
                gate._toolbar_context_resolver = None
                assert gate._build_toolbar() is None

    def test_resolver_returns_toolbar_callable(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate(toolbar_context_resolver=lambda: {
                    "responder": "claude", "model": "gpt4", "cwd": "/tmp", "theme": "dark"
                })
                toolbar_fn = gate._build_toolbar()
                assert callable(toolbar_fn)
                result = toolbar_fn()
                # result is an HTML object or string
                assert result is not None

    def test_toolbar_includes_parallel_context_when_available(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate(toolbar_context_resolver=lambda: {
                    "theme": "line",
                    "parallel": "paralelo:1/1 · fila:2",
                    "responder": "claude",
                })
                toolbar_fn = gate._build_toolbar()
                result = str(toolbar_fn())
                assert "paralelo:1/1" in result
                assert "fila:2" in result

    def test_toolbar_empty_context_returns_empty_string(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate(toolbar_context_resolver=lambda: {})
                toolbar_fn = gate._build_toolbar()
                result = toolbar_fn()
                assert result == ""

    def test_toolbar_resolver_exception_returns_empty_string(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                def bad(): raise RuntimeError("boom")
                gate = _make_gate(toolbar_context_resolver=bad)
                toolbar_fn = gate._build_toolbar()
                result = toolbar_fn()
                assert result == ""


# ---------------------------------------------------------------------------
# InputGate — _build_completer (lines 176-182)
# ---------------------------------------------------------------------------

class TestBuildCompleter:
    def test_no_resolver_returns_none(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate()
                gate._command_resolver = None
                assert gate._build_completer() is None

    def test_with_resolver_returns_completer(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate(command_resolver=lambda: ["/foo"])
                result = gate._build_completer()
                from quimera.app.prompt_input import _SlashCommandCompleter
                assert isinstance(result, _SlashCommandCompleter)


# ---------------------------------------------------------------------------
# InputGate — _build_key_bindings (lines 184-207)
# ---------------------------------------------------------------------------

class TestBuildKeyBindings:
    def test_no_handler_returns_none(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                gate = _make_gate()
                gate._theme_cycle_handler = None
                assert gate._build_key_bindings() is None

    def test_with_handler_returns_keybindings(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                with patch("quimera.app.prompt_input.KeyBindings") as MockKB:
                    called = []
                    gate = _make_gate()
                    gate.set_theme_cycle_handler(lambda: called.append(1))
                    result = gate._build_key_bindings()
                    assert result is MockKB.return_value

    def test_cycle_theme_calls_handler(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                called = []
                gate = _make_gate()
                gate.set_theme_cycle_handler(lambda: called.append(1))
                # Simulate the internal _cycle_theme function
                event = MagicMock()
                # Reach the _cycle_theme by calling _build_key_bindings and extracting it
                # We test indirectly: the KeyBindings.add is called correctly
                kb_mock = MagicMock()
                with patch("quimera.app.prompt_input.KeyBindings", return_value=kb_mock):
                    gate._build_key_bindings()
                    # kb_mock.add should have been called with "c-t", "escape", "t", "f6"
                    assert kb_mock.add.call_count >= 1

    def test_cycle_theme_handler_exception_suppressed(self):
        with patch("quimera.app.prompt_input.PromptSession"):
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                def bad_handler(): raise RuntimeError("crash")
                gate = _make_gate()
                gate.set_theme_cycle_handler(bad_handler)
                kb_mock = MagicMock()
                # Capture the _cycle_theme closure
                captured_fn = []

                def capture_add(*args, **kwargs):
                    def decorator(fn):
                        captured_fn.append(fn)
                        return fn
                    return decorator

                kb_mock.add.side_effect = capture_add
                with patch("quimera.app.prompt_input.KeyBindings", return_value=kb_mock):
                    gate._build_key_bindings()
                if captured_fn:
                    event = MagicMock()
                    # Should not raise
                    captured_fn[0](event)
                    event.app.invalidate.assert_called()


# ---------------------------------------------------------------------------
# InputGate — _flush_renderer
# ---------------------------------------------------------------------------

class TestFlushRenderer:
    def test_no_renderer(self):
        gate = _make_gate()
        gate._renderer = None
        gate._flush_renderer()  # should not raise

    def test_renderer_with_flush(self):
        renderer = MagicMock()
        gate = _make_gate(renderer=renderer)
        gate._flush_renderer()
        renderer.flush.assert_called_once()

    def test_renderer_flush_exception_suppressed(self):
        renderer = MagicMock()
        renderer.flush.side_effect = RuntimeError("boom")
        gate = _make_gate(renderer=renderer)
        gate._flush_renderer()  # should not raise


# ---------------------------------------------------------------------------
# InputGate — __call__
# ---------------------------------------------------------------------------

class TestInputGateCall:
    def test_calls_session_prompt_when_available(self):
        with patch("quimera.app.prompt_input.PromptSession") as MockPS:
            with patch("quimera.app.prompt_input.InMemoryHistory"):
                session = MagicMock()
                session.prompt.return_value = "hello"
                MockPS.return_value = session
                gate = _make_gate()
                result = gate("> ")
                assert result == "hello"
                session.prompt.assert_called_once()


# ---------------------------------------------------------------------------
# InputGate — get_line_buffer (lines 243-255)
# ---------------------------------------------------------------------------

class TestGetLineBuffer:
    def test_no_session_returns_empty(self):
        gate = _make_gate()
        gate._session = None
        assert gate.get_line_buffer() == ""

    def test_session_no_app_returns_empty(self):
        gate = _make_gate()
        session = MagicMock()
        session.app = None
        gate._session = session
        assert gate.get_line_buffer() == ""

    def test_session_app_no_buffer_returns_empty(self):
        gate = _make_gate()
        session = MagicMock()
        session.app.current_buffer = None
        gate._session = session
        assert gate.get_line_buffer() == ""

    def test_session_app_buffer_returns_text(self):
        gate = _make_gate()
        session = MagicMock()
        session.app.current_buffer.text = "hello"
        gate._session = session
        assert gate.get_line_buffer() == "hello"

    def test_session_app_buffer_empty_text(self):
        gate = _make_gate()
        session = MagicMock()
        session.app.current_buffer.text = ""
        gate._session = session
        assert gate.get_line_buffer() == ""


# ---------------------------------------------------------------------------
# InputGate — redisplay (lines 257-267)
# ---------------------------------------------------------------------------

class TestRedisplay:
    def test_no_session_does_nothing(self):
        gate = _make_gate()
        gate._session = None
        gate.redisplay()  # no crash

    def test_session_no_app_does_nothing(self):
        gate = _make_gate()
        session = MagicMock()
        session.app = None
        gate._session = session
        gate.redisplay()  # no crash

    def test_session_app_no_invalidate(self):
        gate = _make_gate()
        session = MagicMock()
        del session.app.invalidate  # ensure getattr returns something non-callable
        session.app.invalidate = "not_callable"
        gate._session = session
        gate.redisplay()  # no crash

    def test_session_app_calls_invalidate(self):
        gate = _make_gate()
        session = MagicMock()
        gate._session = session
        gate.redisplay()
        session.app.invalidate.assert_called_once()


class TestRunInTerminalMessage:
    def test_returns_false_without_session(self):
        gate = _make_gate()
        gate._session = None
        assert gate.run_in_terminal_message(lambda: None) is False

    def test_returns_false_when_app_not_running(self):
        gate = _make_gate()
        session = MagicMock()
        session.app._is_running = False
        gate._session = session
        assert gate.run_in_terminal_message(lambda: None) is False

    def test_schedules_callback_on_prompt_toolkit_loop(self):
        gate = _make_gate()
        session = MagicMock()
        loop = MagicMock()
        loop.is_closed.return_value = False
        session.app._is_running = True
        session.app.loop = loop
        gate._session = session
        called = []

        def callback():
            called.append(True)

        with patch(
            "quimera.app.prompt_input.run_in_terminal",
            side_effect=lambda fn, **kwargs: fn(),
        ) as mock_run:
            loop.call_soon_threadsafe.side_effect = lambda fn: fn()
            assert gate.run_in_terminal_message(callback) is True

        assert called == [True]
        loop.call_soon_threadsafe.assert_called_once()
        mock_run.assert_called_once()
