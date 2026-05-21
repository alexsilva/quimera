"""Tests para persistência e detecção de bugs operacionais."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from unittest.mock import Mock

from quimera.bugs import (
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
