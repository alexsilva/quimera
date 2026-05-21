"""Tests para persistência e detecção de bugs operacionais."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from unittest.mock import Mock

from quimera.bugs import (
    AgentRuntimeBugDetector,
    BugEvidenceRef,
    BugReport,
    BugStore,
    RenderBugDetector,
    format_bug_context,
    make_bug_fingerprint,
)
from quimera.app.core import QuimeraApp


def _build_report(session_id: str, category: str, summary: str) -> BugReport:
    fingerprint = make_bug_fingerprint(session_id, category, summary)
    return BugReport(
        id=f"bug_{fingerprint[:12]}",
        session_id=session_id,
        category=category,
        summary=summary,
        severity="medium",
        confidence=0.8,
        fingerprint=fingerprint,
        evidence_refs=[BugEvidenceRef(kind="render_jsonl", path="/tmp/render.jsonl", line=12)],
    )


def test_bug_store_deduplicates_open_report_by_fingerprint(tmp_path):
    store = BugStore(tmp_path)
    try:
        first = store.file(_build_report("sessao-1", "render_repeat_block", "Bloco ANSI repetido"))
        second = store.file(_build_report("sessao-1", "render_repeat_block", "Bloco ANSI repetido"))
        assert first.id == second.id
        reports = store.query(session_id="sessao-1", status="open")
        assert len(reports) == 1
        assert reports[0].count == 2
    finally:
        store.close()


def test_bug_store_close_bug_marks_status_closed(tmp_path):
    store = BugStore(tmp_path)
    try:
        report = store.file(_build_report("sessao-1", "slot_leak_suspect", "Slots ficaram presos"))
        closed = store.close_bug(report.id)
        assert closed is not None
        assert closed.status == "closed"
        reports = store.query(session_id="sessao-1", status="closed")
        assert len(reports) == 1
        assert reports[0].id == report.id
    finally:
        store.close()


def test_bug_store_file_is_thread_safe_for_same_fingerprint(tmp_path):
    store = BugStore(tmp_path)
    try:
        report = _build_report("sessao-1", "render_repeat_block", "Bloco ANSI repetido")
        with ThreadPoolExecutor(max_workers=8) as pool:
            for _ in range(20):
                pool.submit(store.file, report)
        reports = store.query(session_id="sessao-1", status="open")
        assert len(reports) == 1
        assert reports[0].count == 20
    finally:
        store.close()


def test_bug_store_query_skips_record_with_invalid_types(tmp_path):
    store = BugStore(tmp_path)
    try:
        bad_line = {
            "id": "bug_bad",
            "session_id": "sessao-1",
            "category": "render_repeat_block",
            "summary": "registro inválido",
            "confidence": "not-a-number",
        }
        good = _build_report("sessao-1", "render_repeat_block", "Bloco ANSI repetido").to_dict()
        store.path.write_text(
            json.dumps(bad_line, ensure_ascii=False) + "\n" + json.dumps(good, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        reports = store.query(session_id="sessao-1")
        assert len(reports) == 2
        bad = next(item for item in reports if item.id == "bug_bad")
        assert bad.confidence == 0.5
    finally:
        store.close()


def test_render_bug_detector_scans_events_and_ansi(tmp_path):
    events_path = tmp_path / "render-sessao-1.jsonl"
    ansi_path = tmp_path / "render-sessao-1.ansi"
    events = [
        {"ts": "2026-05-20T00:00:00.000+00:00", "event": "ansi_duplicate_suppressed", "repeats": 3},
        {
            "ts": "2026-05-20T00:00:01.000+00:00",
            "event": "print",
            "preview": "Alex: ⚙ codex TOOLS: executando",
        },
    ]
    events_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8")
    ansi_path.write_text("... _python_exit ... KeyboardInterrupt: ...", encoding="utf-8")

    detector = RenderBugDetector(repeat_threshold=2)
    reports = detector.analyze_session(
        session_id="sessao-1",
        events_path=events_path,
        ansi_path=ansi_path,
        agent="codex",
    )

    categories = {report.category for report in reports}
    assert "render_repeat_block" in categories
    assert "prompt_line_collision" in categories
    assert "interrupt_shutdown_traceback" in categories


def test_render_bug_detector_accepts_missing_events_path(tmp_path):
    ansi_path = tmp_path / "render-sessao-1.ansi"
    ansi_path.write_text("... _python_exit ... KeyboardInterrupt: ...", encoding="utf-8")

    detector = RenderBugDetector(repeat_threshold=2)
    reports = detector.analyze_session(
        session_id="sessao-1",
        events_path=None,
        ansi_path=ansi_path,
        agent="codex",
    )

    assert len(reports) == 1
    assert reports[0].category == "interrupt_shutdown_traceback"


def test_agent_runtime_bug_detector_scans_metrics_and_prompt_pressure(tmp_path):
    metrics_path = tmp_path / "sessao-1.jsonl"
    lines = [
        {"agent": "codex", "total_chars": 62000},
        {"agent": "codex", "total_chars": 64000},
    ]
    metrics_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines),
        encoding="utf-8",
    )
    detector = AgentRuntimeBugDetector(
        min_failures=2,
        min_tool_calls=3,
        latency_threshold_seconds=20.0,
        prompt_total_chars_threshold=60000,
        prompt_threshold_hits=2,
    )
    reports = detector.analyze(
        session_id="sessao-1",
        agent_metrics={
            "codex": {
                "succeeded": 1,
                "failed": 3,
                "latency": 120.0,
                "tool_calls_total": 6,
                "tool_calls_failed": 4,
                "invalid_tool_calls": 2,
                "tool_loop_abortions": 2,
            }
        },
        prompt_metrics_path=metrics_path,
    )
    categories = {item.category for item in reports}
    assert "agent_failure_rate_high" in categories
    assert "agent_latency_high" in categories
    assert "agent_tool_error_burst" in categories
    assert "agent_invalid_tool_calls" in categories
    assert "agent_tool_loop_abort" in categories
    assert "agent_prompt_budget_pressure" in categories


def test_format_bug_context_renders_readable_block(tmp_path):
    store = BugStore(tmp_path)
    try:
        store.file(_build_report("sessao-1", "render_repeat_block", "Bloco ANSI repetido"))
        reports = store.query(session_id="sessao-1", status="open")
        rendered = format_bug_context(reports)
        assert '<bug_context title="Bugs Operacionais Abertos">' in rendered
        assert "[render_repeat_block]" in rendered
        assert "evidence:" in rendered
    finally:
        store.close()


def test_file_bug_persists_without_event_sink(tmp_path):
    app = QuimeraApp.__new__(QuimeraApp)
    app.bug_store = BugStore(tmp_path)
    app.storage = SimpleNamespace(session_id="sessao-1")
    try:
        filed = app._file_bug(
            session_id="sessao-1",
            category="agent_failure_burst",
            summary="Agente codex acumulou falhas consecutivas",
            severity="medium",
            confidence=0.85,
            description="Falhas consecutivas atuais: 2",
            agent="codex",
        )
        assert filed is not None
        reports = app.bug_store.query(session_id="sessao-1", status="open")
        assert len(reports) == 1
        assert reports[0].category == "agent_failure_burst"
    finally:
        app.bug_store.close()


def test_handle_bugs_command_handles_store_errors_without_crashing():
    app = QuimeraApp.__new__(QuimeraApp)
    app.storage = SimpleNamespace(session_id="sessao-1")
    app.show_warning_message = Mock()
    app.show_system_message = Mock()
    app.show_muted_message = Mock()
    app.bug_store = SimpleNamespace(query=Mock(side_effect=RuntimeError("boom")))

    result = app._handle_bugs_command("/bugs list")

    assert result is True
    app.show_warning_message.assert_called_with("[bugs] falha interna ao processar comando.")


def test_handle_bugs_command_stats_renders_aggregates(tmp_path):
    app = QuimeraApp.__new__(QuimeraApp)
    app.storage = SimpleNamespace(session_id="sessao-1")
    app.show_warning_message = Mock()
    app.show_system_message = Mock()
    app.show_muted_message = Mock()
    app.bug_store = BugStore(tmp_path)
    try:
        app.bug_store.file(_build_report("sessao-1", "render_repeat_block", "Bloco ANSI repetido"))
        app.bug_store.file(_build_report("sessao-1", "prompt_line_collision", "Prompt colado"))
        result = app._handle_bugs_command("/bugs stats")
        assert result is True
        payload = app.show_muted_message.call_args[0][0]
        assert "por categoria:" in payload
        assert "render_repeat_block" in payload
        assert "prompt_line_collision" in payload
    finally:
        app.bug_store.close()


def test_render_bug_detector_detects_long_gap(tmp_path):
    events_path = tmp_path / "render-sessao-1.jsonl"
    events = [
        {"ts": "2026-05-20T00:00:00.000+00:00", "event": "print", "preview": "msg1"},
        {"ts": "2026-05-20T00:01:00.000+00:00", "event": "print", "preview": "msg2"},
    ]
    events_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8")

    detector = RenderBugDetector(gap_threshold_seconds=30.0)
    reports = detector.analyze_session(
        session_id="sessao-1",
        events_path=events_path,
        ansi_path=None,
        agent="codex",
    )

    categories = {report.category for report in reports}
    assert "render_long_gap" in categories


def test_render_bug_detector_detects_rapid_burst(tmp_path):
    events_path = tmp_path / "render-sessao-1.jsonl"
    events = [
        {"ts": "2026-05-20T00:00:00.000+00:00", "event": "print", "preview": "msg1"},
        {"ts": "2026-05-20T00:00:00.200+00:00", "event": "print", "preview": "msg2"},
        {"ts": "2026-05-20T00:00:00.400+00:00", "event": "print", "preview": "msg3"},
        {"ts": "2026-05-20T00:00:00.600+00:00", "event": "print", "preview": "msg4"},
        {"ts": "2026-05-20T00:00:00.800+00:00", "event": "print", "preview": "msg5"},
    ]
    events_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8")

    detector = RenderBugDetector(rapid_window_seconds=2.0, rapid_count_threshold=5)
    reports = detector.analyze_session(
        session_id="sessao-1",
        events_path=events_path,
        ansi_path=None,
        agent="codex",
    )

    categories = {report.category for report in reports}
    assert "render_rapid_burst" in categories
