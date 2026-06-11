"""Testes para o texto do infobar de streaming em TerminalRenderer.

Verifica que o hint 'T para tema' foi removido e que 'Ctrl+C para cancelar'
permanece presente tanto no modo single-agent quanto no multi-agent.
"""
import pytest
from unittest.mock import patch
from rich.console import Console
from rich.rule import Rule

from quimera.ui import TerminalRenderer, _extract_text_from_renderable


def _make_capturing_live():
    """Retorna FakeLive e lista que captura os renderables passados ao Live."""
    captured = []

    class FakeLive:
        def __init__(self, renderable, **kwargs):
            captured.append(renderable)

        def start(self):
            pass

        def stop(self):
            pass

        def update(self, renderable, **kwargs):
            captured.append(renderable)

    return FakeLive, captured


@pytest.fixture
def renderer():
    """TerminalRenderer com console de gravação, sem saída real."""
    with patch("quimera.ui._RICH_AVAILABLE", True):
        r = TerminalRenderer()
        r._console = Console(width=80, record=True, force_terminal=False)
        yield r
        r.close(timeout=2.0)


def _has_rule(obj) -> bool:
    """Verifica recursivamente se o renderable contém um Rule."""
    if isinstance(obj, Rule):
        return True
    if hasattr(obj, "renderables"):
        return any(_has_rule(child) for child in obj.renderables)
    return False


class TestStreamingInfobarText:
    """Garante que o infobar não expõe o hint 'T para tema'."""

    def test_single_agent_infobar_omits_tema_hint(self, renderer):
        """Infobar de agente único não contém 'T para tema'."""
        FakeLive, captured = _make_capturing_live()
        with patch("quimera.ui.Live", FakeLive):
            renderer.start_message_stream("claude")
            renderer.update_message_stream("claude", "Processando...")
            renderer.flush()

        assert captured, "renderable deve ter sido capturado pelo Live"
        text = _extract_text_from_renderable(captured[-1])
        assert "T para tema" not in text

    def test_single_agent_infobar_retains_cancel_hint(self, renderer):
        """Infobar de agente único mantém 'Ctrl+C para cancelar'."""
        FakeLive, captured = _make_capturing_live()
        with patch("quimera.ui.Live", FakeLive):
            renderer.start_message_stream("claude")
            renderer.update_message_stream("claude", "Processando...")
            renderer.flush()

        assert captured, "renderable deve ter sido capturado pelo Live"
        text = _extract_text_from_renderable(captured[-1])
        assert "Ctrl+C para cancelar" in text

    def test_multi_agent_infobar_omits_tema_hint(self, renderer):
        """Infobar multi-agente não contém 'T para tema'."""
        FakeLive, captured = _make_capturing_live()
        with patch("quimera.ui.Live", FakeLive):
            renderer.start_message_stream("claude")
            renderer.start_message_stream("codex")
            renderer.update_message_stream("claude", "Chunk A")
            renderer.update_message_stream("codex", "Chunk B")
            renderer.flush()

        assert captured, "renderable deve ter sido capturado pelo Live"
        text = _extract_text_from_renderable(captured[-1])
        assert "T para tema" not in text

    def test_multi_agent_infobar_retains_cancel_hint(self, renderer):
        """Infobar multi-agente mantém 'Ctrl+C para cancelar'."""
        FakeLive, captured = _make_capturing_live()
        with patch("quimera.ui.Live", FakeLive):
            renderer.start_message_stream("claude")
            renderer.start_message_stream("codex")
            renderer.update_message_stream("claude", "Chunk A")
            renderer.update_message_stream("codex", "Chunk B")
            renderer.flush()

        assert captured, "renderable deve ter sido capturado pelo Live"
        text = _extract_text_from_renderable(captured[-1])
        assert "Ctrl+C para cancelar" in text

    def test_compact_density_has_no_infobar_rule(self, renderer):
        """Modo compact não adiciona Rule de infobar ao renderable."""
        renderer._density = "compact"
        FakeLive, captured = _make_capturing_live()
        with patch("quimera.ui.Live", FakeLive):
            renderer.start_message_stream("claude")
            renderer.update_message_stream("claude", "Compacto...")
            renderer.flush()

        if captured:
            assert not _has_rule(captured[-1]), "compact não deve ter infobar Rule"
