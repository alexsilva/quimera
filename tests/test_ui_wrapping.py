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


def test_show_system_folds_long_path_on_narrow_width():
    renderer = TerminalRenderer()
    renderer._console = Console(width=40, record=True, force_terminal=False)

    renderer.show_system(
        "Sessão ativa: /home/alex/PycharmProjects/quimera/data/logs/2026-04-30/"
        "sessao-2026-04-30-222821.jsonl"
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    lines = rendered.splitlines()
    assert lines
    assert "Sessão ativa:" in lines[0]
    assert "sessao-2026-04-30-222821.jsonl" not in lines[0]
    assert "sessao-2026-04-30-222821" in rendered
    assert ".jsonl" in rendered


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


def test_show_turn_summary_compact_layout_folds_long_path_without_ellipsis():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=40, record=True, force_terminal=False)

    long_path = (
        "/home/alex/PycharmProjects/quimera/"
        "some/really/really/really/really/really/really/really/really/long/path_tail_xyz"
    )
    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_3",
            "tools": [
                {
                    "tool": "exec_command_with_really_really_long_name_tail_xyz",
                    "status": "ok",
                    "duration_ms": 42,
                    "input": {"path": long_path},
                }
            ],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "…" not in rendered
    assert "tail_xyz" in rendered


def test_show_turn_summary_wide_layout_folds_long_tool_name_without_ellipsis():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=120, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_4",
            "tools": [
                {
                    "tool": "exec_command_with_really_really_really_really_long_name_tail_xyz",
                    "status": "ok",
                    "duration_ms": 42,
                    "input": {"cmd": "pytest -q"},
                }
            ],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "…" not in rendered
    assert "tail_xyz" in rendered
