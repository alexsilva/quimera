"""Smoke test: TerminalRenderer(theme="rule") com stream lifecycle."""
from unittest.mock import patch

from rich.console import Console

from quimera.ui import TerminalRenderer


def test_rule_stream_lifecycle_does_not_crash():
    """Verifica que Test rule stream lifecycle does not crash."""
    with patch("quimera.ui._RICH_AVAILABLE", True):
        r = TerminalRenderer(theme="rule")
        r._console = Console(width=80, record=True, force_terminal=False)
        r.start_message_stream("codex")
        r.update_message_stream("codex", "Conteúdo chunk 1 ")
        r.update_message_stream("codex", "Conteúdo chunk 2 ")
        r.finish_message_stream("codex", "Conteúdo chunk 1 Conteúdo chunk 2")
        r.flush()
        output = r._console.export_text()
        assert "Conteúdo chunk 1 Conteúdo chunk 2" in output
