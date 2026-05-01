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


def test_show_turn_summary_truncates_long_cmd_in_compact_layout():
    """Cmd muito longo deve ser truncado com '…' no layout compacto para não gerar células altas."""
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=40, record=True, force_terminal=False)

    long_cmd = "/home/alex/PycharmProjects/quimera/.venv/bin/python -m pytest -q --tb=short tests/test_ui.py tests/test_ui_nonregression.py"
    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "t1",
            "tools": [{"tool": "exec_command", "status": "ok", "duration_ms": 120, "input": {"cmd": long_cmd}}],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "…" in rendered
    assert long_cmd not in rendered


def test_show_turn_summary_truncates_long_path_in_compact_layout():
    """Path muito longo deve ser truncado no layout compacto."""
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=40, record=True, force_terminal=False)

    long_path = "/home/alex/PycharmProjects/quimera/very/deep/nested/directory/structure/file.py"
    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "t2",
            "tools": [{"tool": "read_file", "status": "ok", "duration_ms": 5, "input": {"path": long_path}}],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "…" in rendered
    assert long_path not in rendered


def test_show_turn_summary_truncates_long_tool_name_in_compact_layout():
    """Nomes de tool muito longos devem ser truncados no layout compacto."""
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=40, record=True, force_terminal=False)

    long_name = "mcp__filesystem__read_file_and_analyze_content_recursively"
    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "t3",
            "tools": [{"tool": long_name, "status": "ok", "duration_ms": 10, "input": {}}],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "…" in rendered
    assert long_name not in rendered


def test_show_turn_summary_truncates_long_tool_name_in_wide_layout():
    """Nomes de tool muito longos devem ser truncados mesmo no layout wide."""
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=120, record=True, force_terminal=False)

    long_name = "mcp__filesystem__read_file_and_analyze_and_report_content_recursively_deep"
    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "t4",
            "tools": [{"tool": long_name, "status": "ok", "duration_ms": 10, "input": {}}],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "…" in rendered
    assert long_name not in rendered


def test_show_turn_summary_does_not_truncate_normal_content():
    """Conteúdo dentro dos limites não deve ser truncado."""
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=80, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "t5",
            "tools": [{"tool": "exec_command", "status": "ok", "duration_ms": 42, "input": {"cmd": "pytest -q"}}],
        },
    )

    renderer.flush()
    rendered = renderer._console.export_text()
    assert "pytest -q" in rendered
    assert "exec_command" in rendered
    assert "…" not in rendered


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
