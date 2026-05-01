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

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "\n          " not in rendered
    assert "🔮 Claude A correção aplica" in rendered


def test_show_turn_summary_uses_compact_layout_on_narrow_width():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=40, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_1",
            "tools": [
                {
                    "tool": "exec_command",
                    "status": "ok",
                    "duration_ms": 42,
                    "input": {"cmd": "/home/alex/PycharmProjects/quimera/.venv/bin/python -m pytest -q"},
                }
            ],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "Detalhes" not in rendered
    assert "cmd:" in rendered


def test_show_turn_summary_keeps_details_column_on_wide_width():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=120, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_2",
            "tools": [
                {
                    "tool": "exec_command",
                    "status": "ok",
                    "duration_ms": 42,
                    "input": {"cmd": "pytest -q"},
                }
            ],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "Detalhes" in rendered
    assert "cmd: pytest -q" in rendered
