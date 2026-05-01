"""Testes de não-regressão de UI.

Cobre três eixos críticos:
1. Prompt redraw — limpeza e redesenho do prompt do usuário.
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

    def _make_app(self, status="reading", prompt="Alex: "):
        from quimera.app.core import QuimeraApp
        app = QuimeraApp.__new__(QuimeraApp)
        app._nonblocking_input_status = status
        app._nonblocking_prompt_text = prompt
        return app

    def test_no_redraw_when_status_is_idle(self):
        """Status idle → nada é escrito nem redesenhado."""
        app = self._make_app(status="idle")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), \
             patch("quimera.app.core.readline.get_line_buffer", return_value="texto"), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app._redisplay_user_prompt_if_needed()
        mock_write.assert_not_called()

    def test_no_redraw_when_stdin_not_tty(self):
        """stdin não-tty → prompt não é redesenhado."""
        app = self._make_app(status="reading")
        stdin = io.StringIO("")
        stdin.isatty = lambda: False
        with patch("sys.stdin", stdin), \
             patch("quimera.app.core.readline.get_line_buffer", return_value="texto"), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app._redisplay_user_prompt_if_needed()
        mock_write.assert_not_called()

    def test_long_line_buffer_does_not_crash(self):
        """Buffer muito longo não gera exceção."""
        app = self._make_app(status="reading")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        long_buffer = "x" * 2000
        with patch("sys.stdin", stdin), \
             patch("quimera.app.core.readline.get_line_buffer", return_value=long_buffer), \
             patch("sys.stdout.write"), \
             patch("sys.stdout.flush"):
            app._redisplay_user_prompt_if_needed()  # não deve lançar

    def test_clear_first_false_skips_clear_but_writes_line(self):
        """clear_first=False → não emite \\r\\x1b[2K mas ainda escreve o prompt."""
        app = self._make_app(status="reading")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), \
             patch("quimera.app.core.readline.get_line_buffer", return_value="oi"), \
             patch("quimera.app.core.readline.redisplay"), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app._redisplay_user_prompt_if_needed(clear_first=False)
        written = [c.args[0] for c in mock_write.call_args_list]
        assert "\r\x1b[2K" not in written
        assert any("Alex: oi" in w for w in written)

    def test_clear_prompt_not_emitted_when_status_idle(self):
        """_clear_user_prompt_line_if_needed não emite escape quando status é idle."""
        app = self._make_app(status="idle")
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with patch("sys.stdin", stdin), \
             patch("sys.stdout.write") as mock_write, \
             patch("sys.stdout.flush"):
            app._clear_user_prompt_line_if_needed()
        assert call("\r\x1b[2K") not in mock_write.call_args_list


# ===========================================================================
# 2. Long messages
# ===========================================================================

class TestLongMessages:
    """Garante que mensagens longas são tratadas sem crash nem corrupção."""

    def test_show_plain_with_very_long_text_does_not_crash(self, renderer):
        long_text = "palavra " * 200
        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            renderer.show_plain(long_text, agent="codex")
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "palavra" in rendered

    def test_show_plain_without_agent_with_very_long_text(self, renderer):
        long_text = "a" * 500
        renderer.show_plain(long_text)
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "a" * 10 in rendered

    def test_show_system_with_multiline_long_message(self, renderer):
        multiline = "\n".join(f"Linha {i}: " + "conteúdo " * 20 for i in range(10))
        renderer.show_system(multiline)
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "Linha 0" in rendered
        assert "Linha 9" in rendered

    def test_show_message_strips_ansi_from_content(self, renderer):
        ansi_content = "\x1b[31mTexto vermelho\x1b[0m com mais texto"
        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            renderer.show_message("codex", ansi_content)
        renderer.flush()
        rendered = renderer._console.export_text()
        assert "\x1b[" not in rendered
        assert "Texto vermelho" in rendered

    def test_show_message_with_empty_content_does_not_crash(self, renderer):
        with patch("quimera.ui._agent_style", return_value=("cyan", "Codex")):
            renderer.show_message("codex", "")
        renderer.flush()  # não deve lançar

    def test_show_turn_summary_with_very_long_tool_input_does_not_crash(self, renderer):
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
