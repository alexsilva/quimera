from copy import deepcopy
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


def test_show_turn_summary_renders_compact_line_on_narrow_width():
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
    assert "TOOLS: 1 chamadas" in rendered
    assert "último: exec_command(ok)" in rendered
    assert "trace_id=turn_1" in rendered


def test_show_turn_summary_renders_compact_line_on_wide_width():
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
    assert "TOOLS: 1 chamadas" in rendered
    assert "último: exec_command(ok)" in rendered
    assert "trace_id=turn_2" in rendered


def test_show_turn_summary_with_long_tool_name_does_not_crash():
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
    assert "TOOLS: 1 chamadas" in rendered
    assert "tail_xyz" in rendered


def test_show_turn_summary_includes_trace_id_field_when_provided():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=120, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "trace_id": "sessao-xyz:turn_4",
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
    assert "trace_id=sessao-xyz:turn_4" in rendered
    assert "último:" in rendered
    assert "exec_command_with_really_really_really_really_long_name_tail_xyz(ok)" in rendered


def test_show_turn_summary_does_not_mutate_detail_payload():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=80, record=True, force_terminal=False)

    detail = {
        "turn_id": "turn_5",
        "tools": [
            {
                "tool": "exec_command",
                "status": "ok",
                "duration_ms": 42,
                "input": {"cmd": "pytest -q"},
            }
        ],
    }
    original = {
        "turn_id": detail["turn_id"],
        "tools": [dict(detail["tools"][0])],
    }

    renderer.show_turn_summary("codex", detail)
    renderer.flush()

    assert detail == original


def test_show_turn_summary_does_not_mutate_detail_payload_between_layouts():
    detail = {
        "turn_id": "turn_5",
        "tools": [
            {
                "tool": "exec_command",
                "status": "ok",
                "duration_ms": 42,
                "input": {"cmd": "pytest -q"},
            }
        ],
    }
    original = deepcopy(detail)

    compact_renderer = TerminalRenderer(theme="rule")
    compact_renderer._console = Console(width=40, record=True, force_terminal=False)
    compact_renderer.show_turn_summary("codex", detail)
    assert detail == original

    wide_renderer = TerminalRenderer(theme="rule")
    wide_renderer._console = Console(width=120, record=True, force_terminal=False)
    wide_renderer.show_turn_summary("codex", detail)
    assert detail == original


def test_show_turn_summary_skips_non_cli_runtime():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=120, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_non_cli",
            "trace_id": "trace-non-cli",
            "runtime": "openai",
            "tools": [{"tool": "exec_command", "status": "ok", "duration_ms": 42}],
        },
    )
    renderer.flush()

    rendered = renderer._console.export_text()
    assert "TOOLS:" not in rendered
    assert "trace-non-cli" not in rendered


def test_show_turn_summary_cli_runtime_keeps_summary_line():
    renderer = TerminalRenderer(theme="rule")
    renderer._console = Console(width=120, record=True, force_terminal=False)

    renderer.show_turn_summary(
        "codex",
        {
            "turn_id": "turn_cli",
            "trace_id": "trace-cli-123",
            "runtime": "cli",
            "tools": [{"tool": "exec_command", "status": "ok", "duration_ms": 42}],
        },
    )
    renderer.flush()

    rendered = renderer._console.export_text()
    assert "TOOLS: 1 chamadas" in rendered
    assert "trace_id=trace-cli-123" in rendered
