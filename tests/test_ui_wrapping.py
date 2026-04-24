from unittest.mock import patch

from rich.console import Console

from quimera.ui import TerminalRenderer


def test_show_plain_with_agent_wraps_without_second_column_indent():
    renderer = TerminalRenderer()
    renderer._console = Console(width=40, record=True, force_terminal=False)

    with patch("quimera.ui._agent_style", return_value=("magenta", "🔮 Claude")):
        renderer.show_plain(
            "A correção aplica um recorte mais estável do histórico e evita perda de continuidade em respostas longas.",
            agent="claude",
        )

    rendered = renderer._console.export_text()
    assert "\n          " not in rendered
    assert "🔮 Claude A correção aplica" in rendered
