"""Tests for quimera.ui."""
from unittest.mock import patch, MagicMock

import pytest
from rich.console import Console

from quimera.ui import (
    TerminalRenderer,
    _apply_stream_diff,
    _agent_style,
    _normalize_stream_diff,
    strip_ansi,
    _is_interactive_terminal,
)


class TestStripAnsi:
    """Test suite for strip_ansi function."""

    def test_strips_real_ansi_escape(self):
        """Test removal of real ANSI escape sequences."""
        text = "\x1b[31mred text\x1b[0m"
        result = strip_ansi(text)
        assert "red text" in result
        assert "\x1b[" not in result

    def test_strips_orphaned_ansi_brackets(self):
        """Test removal of orphaned ANSI-like brackets."""
        text = "text [1m more [?25h text"
        result = strip_ansi(text)
        assert "text  more  text" in result
        assert "[1m" not in result
        assert "[?25h" not in result

    def test_preserves_rich_markup_like_brackets(self):
        """Test that Rich markup like [bold] is preserved."""
        text = "This is [bold]bold[/bold] text"
        result = strip_ansi(text)
        assert "[bold]" in result
        assert "[/bold]" in result


class TestIsInteractiveTerminal:
    """Test suite for _is_interactive_terminal."""

    @patch("quimera.ui.sys.stdout.isatty")
    @patch("quimera.ui.os.environ.get")
    def test_is_interactive_when_tty_and_not_dumb(self, mock_environ, mock_isatty):
        """Test returns True when TTY and TERM not dumb."""
        mock_isatty.return_value = True
        mock_environ.return_value = "xterm"
        assert _is_interactive_terminal() is True

    @patch("quimera.ui.sys.stdout.isatty")
    @patch("quimera.ui.os.environ.get")
    def test_not_interactive_when_not_tty(self, mock_environ, mock_isatty):
        """Test returns False when not a TTY."""
        mock_isatty.return_value = False
        assert _is_interactive_terminal() is False

    @patch("quimera.ui.sys.stdout.isatty")
    @patch("quimera.ui.os.environ.get")
    def test_not_interactive_when_dumb_term(self, mock_environ, mock_isatty):
        """Test returns False when TERM is dumb."""
        mock_isatty.return_value = True
        mock_environ.return_value = "dumb"
        assert _is_interactive_terminal() is False


class TestAgentStyle:
    """Test suite for _agent_style function."""

    def test_returns_plugin_style(self):
        """Test returns style from injected get_plugin_style callable."""
        def get_plugin_style(agent):
            if agent == "testagent":
                return ("cyan", "🤖 TestAgent")
            return None

        color, label = _agent_style("testagent", get_plugin_style=get_plugin_style)
        assert color == "cyan"
        assert label == "🤖 TestAgent"

    def test_fallback_for_unknown_agent(self):
        """Test fallback when get_plugin_style returns None."""
        color, label = _agent_style("unknownagent")
        assert color == "white"
        assert label == "🤖 Unknownagent"


class TestTerminalRenderer:
    """Test suite for TerminalRenderer."""

    @pytest.fixture
    def renderer_no_rich(self):
        """Create renderer without Rich."""
        with patch("quimera.ui._RICH_AVAILABLE", False):
            return TerminalRenderer()

    @pytest.fixture
    def mock_renderer(self):
        """Create renderer with mocked console."""
        with patch("quimera.ui._RICH_AVAILABLE", True):
            mock_console = MagicMock()
            with patch("quimera.ui.Console", return_value=mock_console):
                renderer = TerminalRenderer()
                renderer._console = mock_console
                return renderer

    def test_show_message_with_rich(self, mock_renderer):
        """Test show_message with Rich available."""
        mock_panel = MagicMock()
        with patch("quimera.ui.Panel", return_value=mock_panel), \
                patch("quimera.ui.Markdown") as mock_md, \
                patch("quimera.ui._agent_style", return_value=("blue", "Test")):
            mock_renderer.show_message("test", "Hello")

    def test_renderer_uses_dynamic_console_width(self):
        """Test renderer does not force a fixed console width."""
        with patch("quimera.ui._RICH_AVAILABLE", True), \
                patch("quimera.ui.Console") as mock_console:
            TerminalRenderer()

        assert "width" not in mock_console.call_args.kwargs

    def test_show_message_without_rich(self, renderer_no_rich, capsys):
        """Test show_message without Rich."""
        renderer_no_rich.show_message("test", "Hello")
        captured = capsys.readouterr()
        assert "Test" in captured.out

    def test_show_no_response_with_rich(self, mock_renderer):
        """Test show_no_response with Rich."""
        with patch("quimera.ui._agent_style", return_value=("blue", "Test")):
            mock_renderer.show_no_response("test")

    def test_show_no_response_without_rich(self, renderer_no_rich, capsys):
        """Test show_no_response without Rich."""
        renderer_no_rich.show_no_response("test")
        captured = capsys.readouterr()
        assert "sem resposta" in captured.out

    def test_show_system_with_rich(self, mock_renderer):
        """Test show_system with Rich."""
        mock_renderer.show_system("System message")
        mock_renderer.flush()
        assert mock_renderer._console.print.called
        rendered_line = mock_renderer._console.print.call_args.args[0]
        assert getattr(rendered_line, "overflow", None) == "fold"

    def test_show_system_with_rich_strips_crlf_edges(self, mock_renderer):
        """Test show_system strips CRLF-only edges to avoid visual line breaks."""
        mock_renderer.show_system("\r\nSystem message\r\n")
        mock_renderer.flush()
        rendered_line = mock_renderer._console.print.call_args.args[0]
        assert rendered_line.plain.startswith("⚙ System message")
        assert not rendered_line.plain.startswith("⚙ \r")

    def test_show_system_without_rich(self, renderer_no_rich, capsys):
        """Test show_system without Rich."""
        renderer_no_rich.show_system("System message")
        captured = capsys.readouterr()
        assert "System message" in captured.out

    def test_show_plain_with_agent(self, mock_renderer):
        """Test show_plain with agent."""
        with patch("quimera.ui._agent_style", return_value=("blue", "Test")):
            mock_renderer.show_plain("Message", agent="test")

    def test_show_plain_with_agent_does_not_pad_label_column(self):
        """Test agent label is rendered without fixed-width gap."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=60, record=True, force_terminal=False)

        with patch("quimera.ui._agent_style", return_value=("blue", "🔷 Codex")):
            renderer.show_plain("execução concluída", agent="codex")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷 Codex execução concluída" in rendered

    def test_show_plain_with_agent_strips_crlf_edges(self):
        """Test show_plain keeps agent label inline when message has CRLF edges."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=60, record=True, force_terminal=False)

        with patch("quimera.ui._agent_style", return_value=("blue", "🔷 Codex")):
            renderer.show_plain("\r\nexecução concluída\r\n", agent="codex")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷 Codex execução concluída" in rendered

    def test_show_plain_without_agent(self, renderer_no_rich, capsys):
        """Test show_plain without agent."""
        renderer_no_rich.show_plain("Plain message")
        captured = capsys.readouterr()
        assert "Plain message" in captured.out

    def test_show_error_with_rich(self, mock_renderer):
        """Test show_error with Rich."""
        mock_renderer.show_error("Error message")

    def test_show_error_without_rich(self, renderer_no_rich, capsys):
        """Test show_error without Rich."""
        renderer_no_rich.show_error("Error message")
        captured = capsys.readouterr()
        assert "Error message" in captured.out

    def test_show_warning_with_rich(self, mock_renderer):
        """Test show_warning with Rich."""
        mock_renderer.show_warning("Warning message")

    def test_show_warning_without_rich(self, renderer_no_rich, capsys):
        """Test show_warning without Rich."""
        renderer_no_rich.show_warning("Warning message")
        captured = capsys.readouterr()
        assert "Warning message" in captured.out

    def test_show_handoff(self, mock_renderer):
        """Test show_handoff display."""
        with patch("quimera.ui._agent_style", side_effect=lambda x, *_: ("white", x.capitalize())):
            mock_renderer.show_handoff("agent1", "agent2", task=123)

    def test_show_handoff_without_task(self, mock_renderer):
        """Test show_handoff without task."""
        with patch("quimera.ui._agent_style", side_effect=lambda x, *_: ("white", x.capitalize())):
            mock_renderer.show_handoff("agent1", "agent2")

    def test_update_status_no_live(self, mock_renderer):
        """Test update_status when no live display."""
        mock_renderer._live = None
        mock_renderer.update_status("agent", "message")

    def test_update_status_with_live(self, mock_renderer):
        """Test update_status with live display."""
        mock_renderer._live = MagicMock()
        mock_renderer._statuses = {}
        mock_renderer.update_status("agent", "message")
        assert mock_renderer._statuses["agent"] == "message"

    def test_render_status_panel(self, mock_renderer):
        """Test _render_status_panel."""
        mock_renderer._statuses = {"agent1": "running", "agent2": "done"}
        with patch("quimera.ui._agent_style", side_effect=lambda x, *_: ("blue", x.capitalize())), \
                patch("quimera.ui.Panel") as mock_panel:
            result = mock_renderer._render_status_panel()
            assert mock_panel.called

    def test_live_status_context_manager(self, mock_renderer):
        """Test live_status context manager."""
        with patch("quimera.ui._RICH_AVAILABLE", True):
            mock_renderer._console = MagicMock()
            mock_renderer._live = None
            with mock_renderer.live_status(["agent1", "agent2"]) as ctx:
                pass

    def test_live_status_without_rich(self, renderer_no_rich):
        """Test live_status without Rich is no-op."""
        with renderer_no_rich.live_status(["agent1"]) as ctx:
            assert ctx is None

    def test_running_status_returns_spinner(self, mock_renderer):
        """Test running_status returns status spinner."""
        mock_renderer._live = None
        mock_renderer._console = MagicMock()
        mock_status = MagicMock()
        mock_renderer._console.status.return_value = mock_status
        with mock_renderer.running_status("Processing...") as ctx:
            assert ctx is not None

    def test_running_status_in_live_mode(self, mock_renderer):
        """Test running_status in live mode returns proxy."""
        mock_renderer._live = MagicMock()
        result = mock_renderer.running_status("Processing...", agent="test")
        assert hasattr(result, "update")

    def test_update_message_stream_accepts_add_diff(self, mock_renderer):
        with patch("quimera.ui.Live") as mock_live_cls:
            mock_live = MagicMock()
            mock_live_cls.return_value = mock_live
            mock_renderer.start_message_stream("codex")
            mock_renderer.update_message_stream("codex", {"diff": [{"op": "add", "text": "def"}]})
            mock_renderer.flush()

        assert mock_live.update.call_count >= 1

    def test_update_message_stream_accepts_replace_diff(self, mock_renderer):
        with patch("quimera.ui.Live") as mock_live_cls:
            mock_live = MagicMock()
            mock_live_cls.return_value = mock_live
            mock_renderer.start_message_stream("codex")
            mock_renderer.update_message_stream("codex", {"diff": [{"op": "replace", "text": "xyz"}]})
            mock_renderer.flush()

        assert mock_live.update.call_count >= 1

    def test_start_message_stream_enables_auto_refresh(self, mock_renderer):
        with patch("quimera.ui.Live") as mock_live:
            mock_live.return_value = MagicMock()
            mock_renderer.start_message_stream("codex")

        assert mock_live.call_args.kwargs["auto_refresh"] is True


class TestRenderOrdering:
    """Testes de integração: garante ordenação de eventos via fila única."""

    @pytest.fixture
    def recording_renderer(self):
        """Renderer com console de gravação para verificar saída."""
        with patch("quimera.ui._RICH_AVAILABLE", True):
            renderer = TerminalRenderer()
            renderer._console = Console(width=80, record=True, force_terminal=False)
            return renderer

    def test_summary_rendered_after_stream_closed(self, recording_renderer):
        """show_turn_summary enfileirada após finish_message_stream é renderizada depois do stream."""
        r = recording_renderer
        with patch("quimera.ui.Live") as mock_live_cls:
            mock_live = MagicMock()
            mock_live_cls.return_value = mock_live
            r.start_message_stream("codex")
            r.update_message_stream("codex", "resposta completa")
            r.finish_message_stream("codex", "resposta completa")
            r.show_turn_summary("codex", {
                "tools": [{"tool": "bash", "status": "ok", "duration_ms": 42}]
            })
            r.flush()

        # live.stop() deve ter sido chamado (stream fechado)
        assert mock_live.stop.called
        # live.update deve ter sido chamado ao menos uma vez (durante update e finish)
        assert mock_live.update.call_count >= 1

    def test_concurrent_prints_no_exception(self, recording_renderer):
        """Múltiplas threads enfileirando prints não geram erros."""
        import concurrent.futures
        r = recording_renderer
        errors = []

        def emit(i):
            try:
                r.show_system(f"mensagem {i}")
            except Exception as exc:
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(emit, range(32)))

        r.flush()
        assert errors == []

    def test_finish_stream_marks_completed_before_enqueue(self, recording_renderer):
        """_completed_streams é populado sync em finish_message_stream, antes do live_stop."""
        r = recording_renderer
        with patch("quimera.ui.Live"):
            r.start_message_stream("codex")
            r.finish_message_stream("codex", "ok")

        # Deve estar em _completed_streams imediatamente (sem precisar de flush)
        assert "codex" in r._completed_streams

    def test_show_message_suppressed_after_stream_finish(self, recording_renderer):
        """show_message com o mesmo conteúdo do stream é suprimido (sem duplicata)."""
        r = recording_renderer
        with patch("quimera.ui.Live"):
            r.start_message_stream("codex")
            r.finish_message_stream("codex", "conteúdo final")
            # show_message com o mesmo conteúdo deve ser suprimido
            r.show_message("codex", "conteúdo final")
            r.flush()

        # _completed_streams deve ter sido consumido pelo show_message
        assert "codex" not in r._completed_streams


class TestStreamingDiffHelpers:
    def test_normalize_stream_diff_accepts_dict(self):
        assert _normalize_stream_diff({"op": "add", "content": "abc"}) == [{"op": "add", "text": "abc"}]

    def test_apply_stream_diff_supports_replace_and_add(self):
        assert _apply_stream_diff("old", [{"op": "replace", "text": "new"}, {"op": "add", "text": "!"}]) == "new!"
