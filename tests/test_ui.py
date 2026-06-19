"""Tests for quimera.ui."""
import threading
from unittest.mock import patch, MagicMock

import pytest
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from quimera import themes
from quimera.ui import (
    TerminalRenderer,
    _apply_stream_diff,
    _agent_style,
    _highlight_tags,
    _normalize_stream_diff,
    _extract_text_from_renderable,
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

    def test_strips_unicode_control_and_bidi_chars(self):
        """Test removal of zero-width and bidi control characters."""
        text = "ab\u200bcd\u202ertl\u2069ef"
        result = strip_ansi(text)
        assert result == "abcdrtlef"


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
                return ("cyan", "🤖  TestAgent")
            return None

        color, label = _agent_style("testagent", get_plugin_style=get_plugin_style)
        assert color == "cyan"
        assert label == "🤖  TestAgent"

    def test_fallback_for_unknown_agent(self):
        """Test fallback when get_plugin_style returns None."""
        color, label = _agent_style("unknownagent")
        assert color == "white"
        assert label == "🤖  Unknownagent"

    def test_fallback_for_none_agent(self):
        """Test fallback is safe when agent is None."""
        color, label = _agent_style(None)
        assert color == "white"
        assert label == "🤖  Unknown"


class TestTerminalRenderer:
    """Test suite for TerminalRenderer."""

    @pytest.fixture
    def renderer_no_rich(self):
        """Create renderer without Rich."""
        with patch("quimera.ui._RICH_AVAILABLE", False):
            renderer = TerminalRenderer()
        yield renderer
        renderer.close(timeout=1.0)

    @pytest.fixture
    def mock_renderer(self):
        """Create renderer with mocked console."""
        with patch("quimera.ui._RICH_AVAILABLE", True):
            mock_console = MagicMock()
            with patch("quimera.ui.Console", return_value=mock_console):
                renderer = TerminalRenderer()
                renderer._console = mock_console
        yield renderer
        renderer.close(timeout=1.0)

    def test_show_message_with_rich(self, mock_renderer):
        """Test show_message with Rich available."""
        mock_panel = MagicMock()
        with patch("quimera.ui.Panel", return_value=mock_panel), \
                patch("quimera.ui.Markdown") as mock_md, \
                patch("quimera.ui._agent_style", return_value=("blue", "Test")):
            mock_renderer.show_message("test", "Hello")

    def test_show_message_extracts_text_from_panel_content(self):
        """Renderable Rich não deve vazar repr interna no conteúdo final."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=80, record=True, force_terminal=False)

        with patch("quimera.ui._agent_style", return_value=("blue", "🔷  Codex")):
            renderer.show_message("codex", Panel(Text("conteudo interno"), title="Titulo"))

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "conteudo interno" in rendered
        assert "rich.panel.Panel object" not in rendered
        renderer.close(timeout=1.0)

    def test_renderer_uses_dynamic_console_width(self):
        """Test renderer does not force a fixed console width."""
        with patch("quimera.ui._RICH_AVAILABLE", True), \
                patch("quimera.ui.Console") as mock_console:
            renderer = TerminalRenderer()
            renderer.close(timeout=1.0)

        assert "width" not in mock_console.call_args.kwargs

    def test_theme_name_exposes_active_theme(self):
        renderer = TerminalRenderer(theme="panel")
        assert renderer.theme_name == "panel"
        renderer.close(timeout=1.0)

    def test_cycle_theme_advances_and_wraps(self):
        ordered_names = themes.names()
        renderer = TerminalRenderer(theme=ordered_names[-1])

        next_name = renderer.cycle_theme()

        assert next_name == ordered_names[0]
        assert renderer.theme_name == ordered_names[0]
        renderer.close(timeout=1.0)

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

    def test_show_system_neutral_with_rich_keeps_text_dimmed(self, mock_renderer):
        """Test neutral system output keeps icon and text in dim style."""
        mock_renderer.show_system_neutral("\r\nSystem message\r\n")
        mock_renderer.flush()
        rendered_line = mock_renderer._console.print.call_args.args[0]
        assert rendered_line.plain.startswith("⚙ System message")
        assert any(span.start == 2 and span.style == "dim" for span in rendered_line.spans)

    def test_suspend_and_resume_output_defers_prints(self, mock_renderer):
        """Durante suspensão externa, prints ficam deferidos até retomar."""
        assert mock_renderer.suspend_output(timeout=1.0) is True

        mock_renderer.show_plain("mensagem deferida")
        mock_renderer.flush(timeout=1.0)
        assert mock_renderer._console.print.call_count == 0

        assert mock_renderer.resume_output(timeout=1.0) is True
        mock_renderer.flush(timeout=1.0)
        assert mock_renderer._console.print.call_count > 0

    def test_suspend_output_sets_flag_before_writer_ack(self, mock_renderer):
        """Mesmo sem ack do writer, o bloqueio deve ser aplicado imediatamente."""
        mock_renderer._output_suspended.clear()
        with patch.object(mock_renderer._queue, "put", autospec=True):
            assert mock_renderer.suspend_output(timeout=0.01) is False
        assert mock_renderer._output_suspended.is_set() is True
        mock_renderer._output_suspended.clear()

    def test_show_banner_with_rich_preserves_ascii_layout(self, mock_renderer):
        """Banner deve evitar wrap para não distorcer logo ASCII."""
        banner = " / __ \\\\__  __(_)___\n/ / / / / / / / __"
        mock_renderer.show_banner(f"\r\n{banner}\r\n")
        mock_renderer.flush()
        first_call_arg = mock_renderer._console.print.call_args_list[0].args[0]
        assert isinstance(first_call_arg, Text)
        assert first_call_arg.no_wrap is True
        assert first_call_arg.overflow == "ignore"
        assert first_call_arg.plain.startswith(" / __ \\\\__")

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

        with patch("quimera.ui._agent_style", return_value=("blue", "🔷  Codex")):
            renderer.show_plain("execução concluída", agent="codex")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex execução concluída" in rendered
        renderer.close(timeout=1.0)

    def test_show_plain_with_agent_strips_crlf_edges(self):
        """Test show_plain keeps agent label inline when message has CRLF edges."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=60, record=True, force_terminal=False)

        with patch("quimera.ui._agent_style", return_value=("blue", "🔷  Codex")):
            renderer.show_plain("\r\nexecução concluída\r\n", agent="codex")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex execução concluída" in rendered
        renderer.close(timeout=1.0)

    def test_show_feed_with_agent_does_not_clear_transient(self):
        """Test show_feed preserves live/transient state while appending to feed."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=60, record=True, force_terminal=False)
        renderer.clear_agent_transient = MagicMock()

        with patch("quimera.ui._agent_style", return_value=("blue", "🔷  Codex")):
            renderer.show_feed("execução concluída", agent="codex")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex execução concluída" in rendered
        renderer.clear_agent_transient.assert_not_called()
        renderer.close(timeout=1.0)

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

    def test_show_error_agent_exit_without_rich_with_agent_label(self, renderer_no_rich, capsys):
        """Test show_error renders agent label before error icon for agent exits."""
        with patch("quimera.ui._agent_style", return_value=("magenta", "🔮  Claude")):
            renderer_no_rich.show_error(
                "",
                agent="claude",
                command_name="claude",
                error_kind="agent_exit",
                return_code=1,
            )
        captured = capsys.readouterr()
        assert "🔮  Claude ✗ [erro] retornou código 1" in captured.out

    def test_show_error_agent_exit_without_rich_without_agent(self, renderer_no_rich, capsys):
        """Test show_error keeps command fallback when agent id is missing."""
        renderer_no_rich.show_error(
            "",
            agent=None,
            command_name="cmd",
            error_kind="agent_exit",
            return_code=1,
        )
        captured = capsys.readouterr()
        assert "[erro] agente cmd retornou código 1" in captured.out

    def test_show_warning_with_rich(self, mock_renderer):
        """Test show_warning with Rich."""
        mock_renderer.show_warning("Warning message")

    def test_show_warning_strips_crlf_edges_keeps_icon_inline(self):
        """Test show_warning keeps warning icon and content on the same line."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=80, record=True, force_terminal=False)

        renderer.show_warning("\nUse /codex <mensagem>\n")
        renderer.flush()

        rendered = renderer._console.export_text()
        assert "⚠ Use /codex <mensagem>" in rendered
        renderer.close(timeout=1.0)

    def test_show_warning_without_rich(self, renderer_no_rich, capsys):
        """Test show_warning without Rich."""
        renderer_no_rich.show_warning("Warning message")
        captured = capsys.readouterr()
        assert "Warning message" in captured.out

    def test_show_prompt_preview_with_rich(self, mock_renderer):
        """Test show_prompt_preview with Rich available."""
        mock_renderer.show_prompt_preview("codex", "PROMPT PREVIEW: codex\n\n<tool>test</tool>\nPROMPT FINAL:\nHello")
        mock_renderer.flush()
        assert mock_renderer._console.print.called
        call_args = mock_renderer._console.print.call_args
        panel = call_args[0][0]
        from rich.panel import Panel
        assert isinstance(panel, Panel)
        assert isinstance(panel.renderable, Text)

    def test_show_prompt_preview_without_rich(self, renderer_no_rich, capsys):
        """Test show_prompt_preview without Rich falls back to stderr."""
        import io
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            renderer_no_rich.show_prompt_preview("codex", "PROMPT PREVIEW: codex\n\nPROMPT FINAL:\nHello")
        stderr_output = mock_stderr.getvalue()
        assert "PROMPT PREVIEW: codex" in stderr_output
        assert "PROMPT FINAL:" in stderr_output

    def test_show_prompt_preview_tags_highlighted_in_panel(self, mock_renderer):
        """Verifica que tags XML no preview são destacadas como Text, não str."""
        mock_renderer.show_prompt_preview("codex", "<tool>hello</tool>")
        mock_renderer.flush()
        call_args = mock_renderer._console.print.call_args
        panel = call_args[0][0]
        assert isinstance(panel.renderable, Text)
        text = panel.renderable
        assert "bold magenta" in str(text.spans) or len(text.spans) > 0

    def test_show_delegation(self, mock_renderer):
        """Test show_delegation display."""
        with patch("quimera.ui._agent_style", side_effect=lambda x, *_: ("white", x.capitalize())):
            mock_renderer.show_delegation("agent1", "agent2", task=123)

    def test_show_delegation_without_task(self, mock_renderer):
        """Test show_delegation without task."""
        with patch("quimera.ui._agent_style", side_effect=lambda x, *_: ("white", x.capitalize())):
            mock_renderer.show_delegation("agent1", "agent2")

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

    def test_render_status_panel_title_includes_agent_count(self, mock_renderer):
        """Painel de status explicita quantos agentes estão ativos."""
        mock_renderer._statuses = {"agent1": "running", "agent2": "done"}

        with patch("quimera.ui._agent_style", side_effect=lambda x, *_: ("blue", x.capitalize())):
            panel = mock_renderer._render_status_panel()

        assert "Agentes em Execução · 2" in _extract_text_from_renderable(panel.title)

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
        mock_renderer._console.status.assert_called_once_with(
            "Processing...",
            refresh_per_second=4,
        )

    def test_running_status_with_agent_formats_identity_without_brackets(self, mock_renderer):
        """Status sequencial inclui identidade do agente sem colchetes artificiais."""
        mock_renderer._live = None
        mock_renderer._console = MagicMock()
        mock_renderer._console.status.return_value = MagicMock()

        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            mock_renderer.running_status("Processing...", agent="codex")

        status_text = mock_renderer._console.status.call_args.args[0]
        assert isinstance(status_text, Text)
        assert status_text.plain == "Codex · Processing..."
        assert "[Codex]" not in status_text.plain

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

    def test_update_agent_transient_prompt_active_renders_each_feed_item(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: True, None)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            renderer.update_agent_transient("codex", "mensagem 1")
            renderer.update_agent_transient("codex", "mensagem 2")
            renderer.update_agent_transient("codex", "mensagem 3")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex mensagem 1" in rendered
        assert "🔷  Codex mensagem 2" in rendered
        assert "🔷  Codex mensagem 3" in rendered
        renderer.close(timeout=1.0)

    def test_update_agent_transient_prompt_active_deduplicates_consecutive_items(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: True, None)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            renderer.update_agent_transient("codex", "$ git status")
            renderer.update_agent_transient("codex", "$ git status")
            renderer.update_agent_transient("codex", "analisando resultado")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert rendered.count("🔷  Codex $ git status") == 1
        assert "🔷  Codex analisando resultado" in rendered
        renderer.close(timeout=1.0)

    def test_update_agent_transient_prompt_active_emits_first_message_after_start_immediately(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: True, None)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            renderer.update_agent_transient("codex", "iniciando execução")
            renderer.update_agent_transient("codex", "primeiro chunk")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex iniciando execução" in rendered
        assert "🔷  Codex primeiro chunk" in rendered
        assert "🔷  Codex … primeiro chunk" not in rendered
        renderer.close(timeout=1.0)

    def test_clear_agent_transient_resets_prompt_feed_buffer(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: True, None)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            renderer.update_agent_transient("codex", "mensagem 1")
            renderer.update_agent_transient("codex", "mensagem 2")
            renderer.clear_agent_transient("codex")
            renderer.update_agent_transient("codex", "mensagem 3")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex mensagem 1" in rendered
        assert "🔷  Codex mensagem 2" in rendered
        assert "🔷  Codex mensagem 3" in rendered
        renderer.close(timeout=1.0)

    def test_clear_agent_transient_keeps_already_printed_prompt_feed_items(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: True, None)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            renderer.update_agent_transient("codex", "mensagem 1")
            renderer.update_agent_transient("codex", "mensagem 2")
            renderer.clear_agent_transient("codex")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex mensagem 1" in rendered
        assert "🔷  Codex mensagem 2" in rendered
        renderer.close(timeout=1.0)

    def test_update_agent_transient_multiple_agents_all_visible_in_combined_feed(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: True, None)

        def mock_style(agent, get_plugin_style=None):
            styles = {"codex": ("cyan", "🔷  Codex"), "gemini": ("green", "🟢  Gemini")}
            return styles.get(agent, ("white", agent))

        with patch("quimera.ui._agent_style", side_effect=mock_style):
            renderer.update_agent_transient("codex", "codex msg 1")
            renderer.update_agent_transient("gemini", "gemini msg 1")
            renderer.update_agent_transient("codex", "codex msg 2")
            renderer.update_agent_transient("gemini", "gemini msg 2")

        renderer.flush()
        rendered = renderer._console.export_text()
        assert "🔷  Codex codex msg 2" in rendered
        assert "🟢  Gemini gemini msg 2" in rendered
        renderer.close(timeout=1.0)

    def test_update_agent_transient_above_prompt_writes_muted_output(self):
        """Output transitório acima do prompt deve ser escrito como muted/dim."""
        import sys

        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)
        writes = []

        def run_above_fn(fn):
            fn()
            return True

        def capture(value):
            writes.append(value)
            return len(value)

        renderer.set_prompt_integration(lambda: True, run_above_fn)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            with patch.object(sys.stdout, "write", side_effect=capture):
                renderer.update_agent_transient("codex", "pensando alto")
                renderer.flush()

        joined = "".join(writes)
        assert "\033[2m🔷  Codex pensando alto\033[0m" in joined
        renderer.close(timeout=1.0)

    def test_cprint_with_run_above_resets_prev_lines_so_next_transient_is_correct(self):
        """_clear_and_print deve resetar _prev_lines[0]=0 ao executar via run_above.
        Sem o fix, o próximo TransientWindowEvent usaria p=N stale e emitiria
        \\033[NA\\033[J apagando a resposta final já impressa."""
        import sys

        renderer = TerminalRenderer()
        renderer._console = Console(width=100, record=True, force_terminal=False)

        def run_above_fn(fn):
            fn()
            return True

        renderer.set_prompt_integration(lambda: True, run_above_fn)

        cursor_ups = []
        original_write = sys.stdout.write

        def capture(s):
            # Captura apenas sequências de cursor-up (\\033[NA)
            import re
            for m in re.findall(r"\033\[(\d+)A", s):
                cursor_ups.append(int(m))
            return original_write(s)

        with patch("quimera.ui._agent_style", return_value=("cyan", "🔷  Codex")):
            with patch.object(sys.stdout, "write", side_effect=capture):
                # Passo 1: janela transitória estabelece prev_lines > 0
                renderer.update_agent_transient("codex", "executando passo 1")
                renderer.flush()

                # Passo 2: _cprint via run_above deve limpar overlay e resetar prev_lines=0
                renderer._print("Resposta final")
                renderer.flush()

                # Passo 3: próximo transitório deve usar prev_lines=0 (não o valor stale)
                renderer.update_agent_transient("codex", "executando passo 2")
                renderer.flush()

        # Se o fix funciona: após _cprint resetar prev_lines=0, o passo 3 usa p=0 →
        # sem cursor-up para a segunda transient. Sem o fix: p=N stale → cursor-up com N.
        # O único cursor-up permitido é o da limpeza em _clear_and_print (passo 2),
        # com valor ≤ 2 (1 msg + trailing \n = 2 linhas).
        assert all(n <= 2 for n in cursor_ups), (
            f"cursor-ups inesperados: {cursor_ups} — overlay double-clear detectado"
        )

        renderer.close(timeout=1.0)

    def test_start_message_stream_disables_auto_refresh(self, mock_renderer):
        # Live é criado no writer thread (_ensure_live) apenas quando há conteúdo;
        # auto_refresh=False elimina repaints ociosos
        with patch("quimera.ui.Live") as mock_live:
            mock_live.return_value = MagicMock()
            mock_renderer.start_message_stream("codex")
            mock_renderer.update_message_stream("codex", "primeiro conteúdo")
            mock_renderer.flush()

        assert mock_live.call_args.kwargs["auto_refresh"] is False

    def test_build_turn_body_uses_text_while_streaming(self, mock_renderer):
        stream_body = mock_renderer._build_turn_body(
            "rule", "Codex", "cyan", "```python\nprint('x')", streaming=True
        )
        final_body = mock_renderer._build_turn_body(
            "rule", "Codex", "cyan", "```python\nprint('x')", streaming=False
        )

        assert isinstance(stream_body, Text)
        assert stream_body.overflow == "fold"
        assert stream_body.no_wrap is False
        assert isinstance(final_body, Markdown)

    def test_build_turn_body_respects_explicit_render_modes(self, mock_renderer):
        plain_body = mock_renderer._build_turn_body(
            "rule", "Codex", "cyan", "```python\nprint('x')", render_mode="plain"
        )
        markdown_body = mock_renderer._build_turn_body(
            "rule", "Codex", "cyan", "texto simples", render_mode="markdown"
        )

        assert isinstance(plain_body, Text)
        assert isinstance(markdown_body, Markdown)

    def test_build_turn_body_auto_mode_defaults_to_markdown(self, mock_renderer):
        body = mock_renderer._build_turn_body(
            "rule", "Codex", "cyan", "texto sem marcação", render_mode="auto"
        )
        assert isinstance(body, Markdown)

    def test_flush_raises_timeout_error_when_writer_does_not_signal(self, mock_renderer):
        with patch.object(threading.Event, "wait", return_value=False):
            with pytest.raises(TimeoutError, match="timed out"):
                mock_renderer.flush()

    def test_flush_prints_deferred_when_prompt_is_active_without_run_above(self):
        renderer = TerminalRenderer()
        renderer._console = MagicMock()
        renderer.set_prompt_integration(lambda: True, None)

        renderer.show_system("mensagem pendente")
        renderer.flush()

        renderer._console.print.assert_called_once()
        renderer.close(timeout=1.0)

    def test_flush_prints_deferred_when_run_above_is_set_but_inactive(self):
        """flush() com force=True deve imprimir mesmo quando run_above está
        configurado mas retorna False (ex: antes do prompt iniciar)."""
        renderer = TerminalRenderer()
        renderer._console = Console(width=80, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: False, lambda cb: False)

        renderer.show_system("banner pendente")
        renderer.flush()

        rendered = renderer._console.export_text()
        assert "banner pendente" in rendered
        renderer.close(timeout=1.0)

    def test_close_flushes_deferred_when_prompt_is_inactive(self):
        renderer = TerminalRenderer()
        renderer._console = Console(width=80, record=True, force_terminal=False)
        renderer.set_prompt_integration(lambda: False, None)

        renderer.show_system("mensagem final")
        renderer.close(timeout=1.0)

        rendered = renderer._console.export_text()
        assert "⚙ mensagem final" in rendered


class TestRenderOrdering:
    """Testes de integração: garante ordenação de eventos via fila única."""

    @pytest.fixture
    def recording_renderer(self):
        """Renderer com console de gravação para verificar saída."""
        with patch("quimera.ui._RICH_AVAILABLE", True):
            renderer = TerminalRenderer()
            renderer._console = Console(width=80, record=True, force_terminal=False)
        yield renderer
        renderer.close(timeout=1.0)

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

    def test_show_turn_summary_without_agent_prefix(self, recording_renderer):
        """Resumo de tools sem agent não deve renderizar prefixo literal None."""
        r = recording_renderer
        r.show_turn_summary(None, {
            "tools": [{"tool": "bash", "status": "ok", "duration_ms": 42}],
            "trace_id": "trace-1",
        })
        r.flush()
        output = r._console.export_text()
        assert "TOOLS: 1 chamadas" in output
        assert "None TOOLS:" not in output

    def test_show_turn_summary_with_agent_uses_agent_style(self, recording_renderer):
        """Resumo de tools com agent deve seguir o estilo visual do agente."""
        r = recording_renderer
        r.show_turn_summary("codex", {
            "tools": [{"tool": "bash", "status": "ok", "duration_ms": 42}],
            "trace_id": "trace-1",
        })

        r.flush()
        output = r._console.export_text()
        assert "TOOLS: 1 chamadas" in output
        assert "Codex TOOLS:" in output
        assert "⚙ codex TOOLS:" not in output

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

    def test_show_message_suppressed_after_stream_finish_with_newline_normalization(self, recording_renderer):
        """Deduplicação do pós-stream ignora diferença de CRLF/trailing spaces."""
        r = recording_renderer
        with patch("quimera.ui.Live"):
            r.start_message_stream("codex")
            r.finish_message_stream("codex", "linha 1\r\nlinha 2  \n")
            r.show_message("codex", "linha 1\nlinha 2")
            r.flush()

        assert "codex" not in r._completed_streams

    def test_coalescing_keeps_order_for_interleaved_agent_events(self, recording_renderer):
        """Cross-agent coalescing: chunks de agentes distintos são batched num único refresh."""
        r = recording_renderer
        lives = []

        class CapturingLive:
            def __init__(self, renderable, console, **kwargs):
                self.console = console
                self.renderables = [renderable]
                lives.append(self)

            def start(self):
                return None

            def update(self, renderable, refresh=True):
                self.renderables.append(renderable)

            def stop(self):
                return None

        def _as_text(renderable):
            c = Console(width=120, record=True, force_terminal=False)
            c.print(renderable)
            return c.export_text()

        with patch("quimera.ui.Live", CapturingLive):
            r.start_message_stream("codex")
            r.start_message_stream("claude")
            r.update_message_stream("codex", "A1")
            r.update_message_stream("codex", "A2")
            r.update_message_stream("claude", "B1")
            r.update_message_stream("codex", "A3")
            r.flush()

        assert len(lives) == 1
        snapshots = [_as_text(renderable) for renderable in lives[0].renderables]
        # Conteúdo final: codex acumulou A1A2A3 e claude acumulou B1 no mesmo snapshot
        assert any("A1A2A3" in snap and "B1" in snap for snap in snapshots)
        # Cross-agent coalescing reduz refreshes: A1A2 e A3 sempre juntos (sem batch intermediário)
        no_intermediate = not any("A1A2" in snap and "A1A2A3" not in snap and "B1" in snap for snap in snapshots)
        assert no_intermediate


class TestStreamingDiffHelpers:
    def test_normalize_stream_diff_accepts_dict(self):
        assert _normalize_stream_diff({"op": "add", "content": "abc"}) == [{"op": "add", "text": "abc"}]

    def test_apply_stream_diff_supports_replace_and_add(self):
        assert _apply_stream_diff("old", [{"op": "replace", "text": "new"}, {"op": "add", "text": "!"}]) == "new!"


class TestExtractTextFromRenderable:
    def test_returns_empty_string_for_none(self):
        assert _extract_text_from_renderable(None) == ""

    def test_returns_string_unchanged(self):
        assert _extract_text_from_renderable("hello") == "hello"

    def test_extracts_plain_from_text(self):
        t = Text("hello world")
        assert _extract_text_from_renderable(t) == "hello world"

    def test_extracts_text_from_group(self):
        g = Group(Text("first"), Text("second"))
        result = _extract_text_from_renderable(g)
        assert "first" in result
        assert "second" in result

    def test_nested_group_extraction(self):
        inner = Group(Text("a"), Text("b"))
        outer = Group(inner, Text("c"))
        result = _extract_text_from_renderable(outer)
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_extracts_text_from_rule_title(self):
        result = _extract_text_from_renderable(Rule("section"))
        assert result == "section"

    def test_extracts_text_from_table_cells(self):
        table = Table()
        table.add_column("Ferramenta")
        table.add_column("Status")
        table.add_row("exec", "ok")
        result = _extract_text_from_renderable(table)
        assert "Ferramenta" in result
        assert "Status" in result
        assert "exec" in result
        assert "ok" in result


class TestHighlightTags:
    """Test suite for _highlight_tags function."""

    def test_simple_tag(self):
        result = _highlight_tags("<tool>")
        assert isinstance(result, Text)
        assert result.plain == "<tool>"
        assert len(result.spans) == 1

    def test_closing_tag(self):
        result = _highlight_tags("</tool>")
        assert isinstance(result, Text)
        assert result.plain == "</tool>"
        assert len(result.spans) == 1

    def test_self_closing_tag(self):
        result = _highlight_tags('<tool name="x"/>')
        assert isinstance(result, Text)
        assert result.plain == '<tool name="x"/>'
        assert len(result.spans) == 1

    def test_plain_text_no_tags(self):
        result = _highlight_tags("hello world")
        assert isinstance(result, Text)
        assert result.plain == "hello world"
        assert len(result.spans) == 0

    def test_mixed_content(self):
        result = _highlight_tags("a <tool>b</tool> c")
        assert isinstance(result, Text)
        assert result.plain == "a <tool>b</tool> c"
        assert len(result.spans) == 2

    def test_empty_string(self):
        result = _highlight_tags("")
        assert isinstance(result, Text)
        assert result.plain == ""

    def test_rich_markup_is_not_confused_with_tags(self):
        result = _highlight_tags("[bold]text[/bold]")
        assert isinstance(result, Text)
        assert result.plain == "[bold]text[/bold]"
        assert len(result.spans) == 0

    def test_lone_angle_bracket_not_tag(self):
        result = _highlight_tags("a < b > c")
        assert isinstance(result, Text)
        assert result.plain == "a < b > c"
        assert len(result.spans) == 0
