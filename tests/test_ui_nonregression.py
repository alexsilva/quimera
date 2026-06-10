"""Testes de não-regressão de UI.

Cobre três eixos críticos:
1. Prompt redraw — redesenho do prompt do usuário.
2. Long messages — textos/inputs longos não travam nem corrompem saída.
3. Stream/summary sequence — ordenação e isolamento entre agentes e fluxo stream→resumo.
"""
import io
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from rich.console import Console

from quimera.ui import TerminalRenderer


# ---------------------------------------------------------------------------
# Fixtures compartilhadas
# ---------------------------------------------------------------------------

@pytest.fixture
def renderer():
    with patch("quimera.ui._RICH_AVAILABLE", True):
        r = TerminalRenderer()
        r._console = Console(width=80, record=True, force_terminal=False)
        return r


# ===========================================================================
# 1. Prompt redraw
# ===========================================================================

class TestPromptRedraw:
    """Garante que o redraw do prompt ocorre apenas quando adequado."""

    def _make_app(self, status="reading", prompt="Você: "):
        from quimera.app.core import QuimeraApp
        app = QuimeraApp.__new__(QuimeraApp)
        app.runtime_state.nonblocking_input_status = status
        app.runtime_state.nonblocking_prompt_text = prompt
        app.input_gate = MagicMock()
        app.input_gate.get_line_buffer.return_value = ""
        app.input_gate.is_active.return_value = status == "reading"
        app.input_gate.has_session.return_value = False
        return app

    def test_no_redraw_when_status_is_idle(self):
        """Status idle → nada é escrito nem redesenhado."""
        app = self._make_app(status="idle")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app.input_gate.get_line_buffer.return_value = "texto"
            app._redisplay_user_prompt_if_needed()
        mock_write.assert_not_called()

    def test_no_redraw_when_stdin_not_tty(self):
        """stdin não-tty → prompt não é redesenhado."""
        app = self._make_app(status="reading")
        stdin = io.StringIO("")
        stdin.isatty = lambda: False
        with patch("sys.stdin", stdin), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app.input_gate.get_line_buffer.return_value = "texto"
            app._redisplay_user_prompt_if_needed()
        mock_write.assert_not_called()

    def test_long_line_buffer_does_not_crash(self):
        """Buffer muito longo não gera exceção."""
        app = self._make_app(status="reading")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        long_buffer = "x" * 2000
        with patch("sys.stdin", stdin), \
             patch("sys.stdout.write"), \
             patch("sys.stdout.flush"):
            app.input_gate.get_line_buffer.return_value = long_buffer
            app._redisplay_user_prompt_if_needed()  # não deve lançar

    def test_clear_first_false_uses_prompt_toolkit_redisplay_only(self):
        """clear_first=False mantém redraw apenas via redisplay, sem escrita manual."""
        app = self._make_app(status="reading")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app.input_gate.get_line_buffer.return_value = "oi"
            app._redisplay_user_prompt_if_needed(clear_first=False)
        written = [c.args[0] for c in mock_write.call_args_list]
        assert "\r\x1b[2K" not in written
        assert not any("Você: oi" in w for w in written)
        app.input_gate.redisplay.assert_called_once()

    def test_prompt_toolkit_session_redisplay_skips_manual_prompt_rewrite(self):
        """Com sessão prompt_toolkit, o redraw deve ser só via redisplay."""
        app = self._make_app(status="reading")
        class _PromptToolkitGate:
            def __init__(self):
                self.redisplay = MagicMock()

            @staticmethod
            def has_session() -> bool:
                return True

            @staticmethod
            def is_active() -> bool:
                return True

            @staticmethod
            def get_line_buffer() -> str:
                return "oi"

        app.input_gate = _PromptToolkitGate()
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app._redisplay_user_prompt_if_needed()
        written = [c.args[0] for c in mock_write.call_args_list]
        assert not any("Você: oi" in w for w in written)
        assert "\r\x1b[2K" not in written
        app.input_gate.redisplay.assert_called_once()


# ===========================================================================
# 2. Long messages
# ===========================================================================

class TestLongMessages:
    """Garante que mensagens longas são tratadas sem crash nem corrupção."""

    def test_show_plain_with_very_long_text_does_not_crash(self, renderer):
        """Verifica que Test show plain with very long text does not crash."""
        long_text = "palavra " * 200
        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            renderer.show_plain(long_text, agent="codex")
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "palavra" in rendered

    def test_show_plain_without_agent_with_very_long_text(self, renderer):
        """Verifica que Test show plain without agent with very long text."""
        long_text = "a" * 500
        renderer.show_plain(long_text)
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "a" * 10 in rendered

    def test_show_system_with_multiline_long_message(self, renderer):
        """Verifica que Test show system with multiline long message."""
        multiline = "\n".join(f"Linha {i}: " + "conteúdo " * 20 for i in range(10))
        renderer.show_system(multiline)
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "Linha 0" in rendered
        assert "Linha 9" in rendered

    def test_show_message_strips_ansi_from_content(self, renderer):
        """Verifica que Test show message strips ansi from content."""
        ansi_content = "\x1b[31mTexto vermelho\x1b[0m com mais texto"
        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            renderer.show_message("codex", ansi_content)
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "\x1b[" not in rendered
        assert "Texto vermelho" in rendered

    def test_show_message_with_empty_content_does_not_crash(self, renderer):
        """Verifica que Test show message with empty content does not crash."""
        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            renderer.show_message("codex", "")
        renderer.flush()  # não deve lançar

    def test_show_turn_summary_with_very_long_tool_input_does_not_crash(self, renderer):
        """Verifica que Test show turn summary with very long tool input does not crash."""
        long_cmd = "python " + " ".join(f"--arg{i}=valor_{i}" for i in range(60))
        renderer.show_turn_summary(
            "codex",
            {
                "turn_id": "turn_long",
                "tools": [
                    {
                        "tool": "exec_command",
                        "status": "ok",
                        "duration_ms": 100,
                        "input": {"cmd": long_cmd},
                    }
                ],
            },
        )
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "exec_command" in rendered


# ===========================================================================
# 3. Stream / summary sequence
# ===========================================================================

class TestStreamSummarySequence:
    """Garante ordenação e isolamento corretos entre stream e resumo."""

    @pytest.fixture
    def rec(self):
        with patch("quimera.ui._RICH_AVAILABLE", True):
            r = TerminalRenderer()
            r._console = Console(width=80, record=True, force_terminal=False)
            return r

    def test_abort_stream_does_not_suppress_show_message(self, rec):
        """abort_message_stream não marca completed → show_message não é suprimido."""
        with patch("quimera.ui.Live"):
            rec.start_message_stream("codex")
            rec.abort_message_stream("codex")
            rec.show_message("codex", "conteúdo após abort")
            rec.flush()
        rendered = rec._console.export_text()
        assert "conteúdo após abort" in rendered

    def test_finish_stream_isolates_agents(self, rec):
        """finish_message_stream de um agente não afeta _completed_streams do outro."""
        with patch("quimera.ui.Live"):
            rec.start_message_stream("claude")
            rec.start_message_stream("codex")
            rec.finish_message_stream("claude", "resposta claude")

        assert "claude" in rec._completed_streams
        assert "codex" not in rec._completed_streams

    def test_show_turn_summary_without_stream_does_not_crash(self, rec):
        """show_turn_summary pode ser chamado sem stream precedente."""
        rec.show_turn_summary(
            "claude",
            {
                "turn_id": "turn_no_stream",
                "tools": [{"tool": "bash", "status": "ok", "duration_ms": 5}],
            },
        )
        rec.flush()
        rendered = rec._console.export_text()
        assert "bash" in rendered

    def test_stream_content_then_show_message_different_content_not_suppressed(self, rec):
        """show_message com conteúdo DIFERENTE do stream não é suprimido."""
        with patch("quimera.ui.Live"):
            rec.start_message_stream("claude")
            rec.finish_message_stream("claude", "resposta original")
            rec.show_message("claude", "resposta diferente")
            rec.flush()
        rendered = rec._console.export_text()
        assert "resposta diferente" in rendered

    def test_multiple_finish_stream_same_agent_second_cleans_up(self, rec):
        """Chamar finish_message_stream duas vezes não deixa estado inconsistente."""
        with patch("quimera.ui.Live"):
            rec.start_message_stream("codex")
            rec.finish_message_stream("codex", "conteúdo final")
            rec.finish_message_stream("codex", "conteúdo final")  # segunda chamada

        # _completed_streams não deve ter duplicatas (só str simples, não lista)
        assert isinstance(rec._completed_streams.get("codex"), str)

    def test_summary_after_stream_flush_renders_tool_name(self, rec):
        """show_turn_summary enfileirado após finish_message_stream aparece na saída."""
        with patch("quimera.ui.Live"):
            rec.start_message_stream("codex")
            rec.update_message_stream("codex", "processando...")
            rec.finish_message_stream("codex", "processando...")
            rec.show_turn_summary(
                "codex",
                {
                    "turn_id": "t1",
                    "tools": [{"tool": "write_file", "status": "ok", "duration_ms": 12}],
                },
            )
            rec.flush()
        rendered = rec._console.export_text()
        assert "write_file" in rendered

    def test_abort_stream_then_error_does_not_leave_blank_line(self, rec):
        """Fechar stream antes de erro não deve deixar linha vazia extra."""
        rec.start_message_stream("claude")
        rec.abort_message_stream("claude")
        rec.show_plain("You've hit your limit", agent="claude")
        rec.show_error("[erro] retornou código 1", agent="claude")
        rec.flush()
        rendered = rec._console.export_text()
        assert "You've hit your limit\n\n" not in rendered

    def test_show_message_after_system_neutral_skips_extra_spacing(self, rec):
        """Mensagem final após status neutro não abre linha em branco redundante."""
        rec.show_system_neutral("[fallback] claude não respondeu; codex assumiu")
        rec.show_message("codex", "Commit criado")
        rec.flush()
        rendered = rec._console.export_text()
        assert "assumiu\n\n" not in rendered
