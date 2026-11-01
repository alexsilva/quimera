"""Rodapé do turno: wall-clock em vez de soma, contagens sem redundância e delegações."""
from unittest.mock import Mock

import pytest
from rich.console import Console

from quimera.domain.task_states import Visibility
from quimera.domain.tool_activity import (
    ToolActivityCategory,
    classify_tool_activity,
    count_tool_activities,
)
from quimera.spy_output_presenter import SpyOutputPresenter
from quimera.ui.messages import format_duration, format_turn_summary_lines
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.renderables import _render_event
from quimera.ui.textual.renderer import TextualRenderer


def _renderer() -> tuple[TextualRenderer, list[TextualUiEvent]]:
    bridge = TextualUiBridge()
    emitted: list[TextualUiEvent] = []
    bridge.emit = emitted.append
    return TextualRenderer(bridge), emitted


def _render_text(event: TextualUiEvent) -> str:
    console = Console(record=True, width=160)
    console.print(_render_event(event))
    return console.export_text()


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (0, "0ms"),
        (999, "999ms"),
        (1500, "1.5s"),
        (1_050, "1.1s"),
        (59_900, "59.9s"),
        (59_949, "59.9s"),
        (59_950, "1m00s"),
        (59_999, "1m00s"),
        (60_000, "1m00s"),
        (60_500, "1m01s"),
        (171_977, "2m52s"),
        (3_599_499, "59m59s"),
        (3_599_500, "1h00m"),
        (3_599_999, "1h00m"),
        (3_600_000, "1h00m"),
        (3_900_000, "1h05m"),
        (171_977_900, "47h46m"),
    ],
)
def test_format_duration_bands(duration_ms, expected):
    assert format_duration(duration_ms) == expected


def test_renderer_turn_summary_prefers_elapsed_over_sum():
    renderer, emitted = _renderer()

    renderer.show_turn_summary(
        "claude",
        {
            "runtime": "cli",
            "elapsed_ms": 10_000,
            "tools": [
                {"tool": "tasks_delegate", "status": "ok", "duration_ms": 10_000},
                {"tool": "tasks_delegate", "status": "ok", "duration_ms": 10_000},
            ],
        },
    )

    payload = emitted[-1].payload
    assert payload["duration"] == "10.0s"
    assert payload["duration_ms"] == 10_000
    assert "elapsed_ms" not in payload


def test_renderer_turn_summary_falls_back_to_sum_without_elapsed():
    renderer, emitted = _renderer()

    renderer.show_turn_summary(
        "claude",
        {
            "runtime": "cli",
            "tools": [
                {"status": "ok", "duration_ms": 400},
                {"status": "ok", "duration_ms": 700},
            ],
        },
    )

    payload = emitted[-1].payload
    assert payload["duration"] == "1.1s"
    assert payload["duration_ms"] == 1100


def test_plain_turn_summary_prefers_elapsed_over_sum():
    lines = format_turn_summary_lines(
        {
            "elapsed_ms": 10_000,
            "tools": [
                {"status": "ok", "duration_ms": 10_000},
                {"status": "ok", "duration_ms": 10_000},
            ],
        }
    )

    assert lines == ["TOOLS: 2 chamadas · 2 ok · 0 erro · 10.0s"]


def test_presenter_turn_detail_carries_wall_clock_since_turn_start(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("quimera.spy_output_presenter.time.monotonic", lambda: now[0])
    presenter = SpyOutputPresenter(Mock(), Visibility.SUMMARY)

    now[0] = 500.0
    presenter.reset()
    now[0] = 512.345
    detail = presenter.finalize_turn("claude")

    assert detail["elapsed_ms"] == 12_345


def test_presenter_sends_elapsed_ms_to_renderer(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("quimera.spy_output_presenter.time.monotonic", lambda: now[0])
    renderer = Mock()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)
    presenter.set_turn_runtime("openai")
    presenter.record_tool_call("tasks_delegate", {"agent": "codex"}, "call-1")
    presenter.record_tool_call("tasks_delegate", {"agent": "gemini"}, "call-2")
    now[0] = 10.0

    presenter.finalize_turn("claude", render_summary=True)

    renderer.show_turn_summary.assert_called_once()
    _, detail = renderer.show_turn_summary.call_args.args
    assert detail["elapsed_ms"] == 10_000
    assert len(detail["tools"]) == 2


def test_footer_hides_failures_and_redundant_completed_when_all_ok():
    text = _render_text(
        TextualUiEvent(
            "turn_summary",
            {
                "total": 25,
                "ok_count": 25,
                "err_count": 0,
                "duration": "2m51s",
                "activity_counts": {"delegation": 25},
            },
            agent="claude",
        )
    )

    assert "25 ferramentas · 25 delegações · 2m51s" in text
    assert "falha" not in text
    assert "concluída" not in text
    assert "execuç" not in text


def test_footer_shows_failures_when_present():
    text = _render_text(
        TextualUiEvent(
            "turn_summary",
            {
                "total": 25,
                "ok_count": 23,
                "err_count": 2,
                "duration": "2m51s",
                "activity_counts": {"delegation": 25},
            },
            agent="claude",
        )
    )

    assert "25 ferramentas · 2 falhas · 25 delegações · 2m51s" in text
    assert "concluída" not in text


def test_footer_keeps_completed_when_some_tools_have_no_outcome():
    text = _render_text(
        TextualUiEvent(
            "turn_summary",
            {"total": 3, "ok_count": 1, "err_count": 1, "duration": "1.0s"},
            agent="claude",
        )
    )

    assert "3 ferramentas · 1 falha · 1 concluída · 1.0s" in text


def _live_mcp_card_text(tool_total: int, ok_count: int, err_count: int) -> str:
    return _render_text(
        TextualUiEvent(
            "agent_update",
            {
                "content": "",
                "label": "mcp-http",
                "style": "cyan",
                "theme": "chat",
                "transport": "mcp_http",
                "tools": ["◇ ⚒ git_status"],
                "tool_total": tool_total,
                "tool_ok_count": ok_count,
                "tool_err_count": err_count,
                "tool_duration_ms": 1200,
            },
            agent="mcp-http",
        )
    )


def test_live_mcp_card_hides_completed_when_all_ok():
    text = _live_mcp_card_text(2, 2, 0)

    assert "2 ferramentas · 1.2s" in text
    assert "concluída" not in text
    assert "falha" not in text


def test_live_mcp_card_keeps_completed_when_some_tools_have_no_outcome():
    text = _live_mcp_card_text(2, 1, 0)

    assert "2 ferramentas · 1 concluída · 1.2s" in text


def test_live_mcp_card_shows_failures_like_footer():
    text = _live_mcp_card_text(2, 1, 1)

    assert "2 ferramentas · 1 falha · 1.2s" in text
    assert "concluída" not in text


@pytest.mark.parametrize(
    "tool_name",
    ["tasks_delegate", "tasks_create", "workflows_start", "workflows_replan", "mcp__quimera__tasks_delegate"],
)
def test_delegation_tools_classify_as_delegation(tool_name):
    assert classify_tool_activity(tool_name) is ToolActivityCategory.DELEGATION


def test_workflows_create_is_not_delegation():
    assert classify_tool_activity("workflows_create") is ToolActivityCategory.EXECUTION


def test_count_tool_activities_counts_delegations():
    counts = count_tool_activities([{"tool": "tasks_delegate"}, {"tool": "tasks_delegate"}, {"tool": "read_file"}])

    assert counts == {"delegation": 2, "inspection": 1}


def _mcp_tool(model: TextualFeedModel, msg_id: str, *, preview: bool, finish_ms: int | None = None) -> None:
    common = {"session_id": "session-1", "client_name": "claude-code", "transport": "mcp_http"}
    if preview:
        model.apply(TextualUiEvent(
            "tool_preview",
            {**common, "content": "⚒ tasks_delegate", "mcp_msg_id": msg_id},
            agent="claude-fable",
        ))
    if finish_ms is not None:
        model.apply(TextualUiEvent(
            "tool_state",
            {
                **common,
                "msg_id": msg_id,
                "tool_name": "tasks_delegate",
                "status": "finished",
                "duration_ms": finish_ms,
            },
            agent="claude-fable",
        ))


def test_mcp_burst_overlapping_tools_use_wall_clock_not_sum():
    now = [100.0]
    model = TextualFeedModel(clock=lambda: now[0])

    _mcp_tool(model, "1", preview=True)
    now[0] = 100.5
    _mcp_tool(model, "2", preview=True)
    now[0] = 110.0
    _mcp_tool(model, "1", preview=False, finish_ms=10_000)
    now[0] = 110.5
    _mcp_tool(model, "2", preview=False, finish_ms=10_000)

    live_payload = model.items[-1].event.payload
    assert live_payload["tool_total"] == 2
    assert live_payload["tool_duration_ms"] == 10_500

    assert model.apply(TextualUiEvent(
        "agent_message",
        {"content": "Resposta final.", "run_id": "agentrun:1"},
        agent="claude-fable",
    ))
    footer = model.items[-1].event.payload["turn_summary"]
    assert footer == {"total": 2, "ok_count": 2, "err_count": 0, "duration": "10.5s"}


def test_mcp_burst_without_preview_estimates_start_from_tool_duration():
    now = [50.0]
    model = TextualFeedModel(clock=lambda: now[0])

    _mcp_tool(model, "1", preview=False, finish_ms=10_000)
    now[0] = 51.0
    _mcp_tool(model, "2", preview=False, finish_ms=10_000)

    (stats,) = model._mcp_http_tool_stats_by_agent.values()
    assert stats["duration_ms"] == 11_000
