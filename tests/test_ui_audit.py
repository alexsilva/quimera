import json
from unittest.mock import patch

from quimera.agent_events import SpyEvent
from quimera.constants import Visibility
from quimera.spy_output_presenter import SpyOutputPresenter
from quimera.ui import RenderAuditLogger, TerminalRenderer


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_renderer_audit_logs_static_print_and_ansi(tmp_path):
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-test-session.jsonl"
    ansi_path = log_dir / "render-test-session.ansi"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
    try:
        renderer.show_system("System message")
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    event_names = [event["event"] for event in events]
    assert "print" in event_names
    assert any("System message" in event.get("preview", "") for event in events)
    assert "System message" in ansi_path.read_text(encoding="utf-8")


def test_renderer_audit_logs_stream_lifecycle(tmp_path):
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-test-session.jsonl"
    ansi_path = log_dir / "render-test-session.ansi"
    with patch("quimera.ui._is_interactive_terminal", return_value=True):
        renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
        try:
            renderer.start_message_stream("codex")
            renderer.update_message_stream("codex", {"text": "partial"})
            renderer.finish_message_stream("codex", "final")
            renderer.flush()
        finally:
            renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    event_names = [event["event"] for event in events]
    assert "stream_start" in event_names
    assert "stream_chunk" in event_names
    assert "stream_stop" in event_names
    assert any(event.get("agent") == "codex" for event in events if event["event"].startswith("stream_"))
    assert ansi_path.read_bytes()


def test_audit_preview_extracts_text_from_rich_group(tmp_path):
    """Previews must show human-readable text, not '<rich.console.Group object at 0x...>'."""
    from rich.console import Group
    from rich.text import Text

    log_dir = tmp_path / "render"
    events_path = log_dir / "render-group-test.jsonl"
    ansi_path = log_dir / "render-group-test.ansi"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
    try:
        group = Group(Text("Hello"), Text("World"))
        renderer._print(group)
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    print_events = [e for e in events if e["event"] == "print"]
    assert len(print_events) == 1
    preview = print_events[0].get("preview", "")
    assert "Hello" in preview
    assert "World" in preview
    assert "<rich.console.Group" not in preview
    assert "object at" not in preview


def test_audit_preview_extracts_text_from_table_and_rule_group(tmp_path):
    """Prompt-preview-like groups must not leak Rich object reprs into audit logs."""
    from rich.console import Group
    from rich.rule import Rule
    from rich.table import Table

    log_dir = tmp_path / "render"
    events_path = log_dir / "render-table-rule-test.jsonl"
    ansi_path = log_dir / "render-table-rule-test.ansi"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
    try:
        table = Table()
        table.add_column("Tool")
        table.add_column("Status")
        table.add_row("exec", "ok")
        renderer._print(Group(table, Rule("Prompt Preview")))
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    print_events = [e for e in events if e["event"] == "print"]
    assert len(print_events) == 1
    preview = print_events[0].get("preview", "")
    assert "Tool" in preview
    assert "Status" in preview
    assert "exec" in preview
    assert "ok" in preview
    assert "Prompt Preview" in preview
    assert "object at" not in preview


def test_audit_no_empty_print_events(tmp_path):
    """Empty prints must not produce audit entries (they add no debug value)."""
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-empty-test.jsonl"
    ansi_path = log_dir / "render-empty-test.ansi"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
    try:
        renderer._print("")
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    print_events = [e for e in events if e["event"] == "print"]
    assert len(print_events) == 0


def test_audit_no_noop_events(tmp_path):
    """NoopEvent (used for flush sync) must not produce audit entries."""
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-noop-test.jsonl"
    ansi_path = log_dir / "render-noop-test.ansi"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
    try:
        renderer.show_system("test")
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    noop_events = [e for e in events if e["event"] == "noop"]
    assert len(noop_events) == 0


def test_ansi_burst_duplicates_are_suppressed_and_logged(tmp_path):
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-dedup-test.jsonl"
    ansi_path = log_dir / "render-dedup-test.ansi"
    logger = RenderAuditLogger(events_path, ansi_path)
    payload = ("⚙ Audit de render:\n  /tmp/quimera/render-session.jsonl\n" * 2).encode("utf-8")
    with patch("quimera.ui.audit.time.monotonic", side_effect=[1.0, 1.01, 1.02, 1.03, 2.0]):
        logger.write_ansi(payload)
        logger.write_ansi(payload)
        logger.write_ansi(payload)
        logger.write_ansi(payload)
        logger.write_ansi(b"next\n")
    logger.close()

    assert ansi_path.read_bytes() == payload + b"next\n"
    events = _read_jsonl(events_path)
    dedup_events = [e for e in events if e.get("event") == "ansi_duplicate_suppressed"]
    assert len(dedup_events) == 1
    assert dedup_events[0].get("repeats") == 3
    assert int(dedup_events[0].get("payload_bytes", 0)) == len(payload)


def test_ansi_dedup_does_not_suppress_short_payload(tmp_path):
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-dedup-short-test.jsonl"
    ansi_path = log_dir / "render-dedup-short-test.ansi"
    logger = RenderAuditLogger(events_path, ansi_path)
    payload = b"short\n"
    with patch("quimera.ui.audit.time.monotonic", side_effect=[1.0, 1.01]):
        logger.write_ansi(payload)
        logger.write_ansi(payload)
    logger.close()

    assert ansi_path.read_bytes() == payload + payload
    events = _read_jsonl(events_path)
    dedup_events = [e for e in events if e.get("event") == "ansi_duplicate_suppressed"]
    assert len(dedup_events) == 0


def test_renderer_audit_logs_transient_updates_and_chunk_previews(tmp_path):
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-transient-test.jsonl"
    ansi_path = log_dir / "render-transient-test.ansi"
    with patch("quimera.ui._is_interactive_terminal", return_value=True):
        renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
        try:
            renderer.update_agent_transient("codex", "pensando: validar renderer")
            renderer.clear_agent_transient("codex")
            renderer.flush()
        finally:
            renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    transient_updates = [e for e in events if e.get("event") == "transient_update"]
    assert transient_updates
    assert transient_updates[0].get("agent") == "codex"
    assert "pensando: validar renderer" in str(transient_updates[0].get("preview", ""))

    stream_chunks = [e for e in events if e.get("event") == "stream_chunk"]
    assert stream_chunks
    previews = stream_chunks[0].get("previews")
    assert isinstance(previews, list)
    assert any("pensando: validar renderer" in str(item) for item in previews)


def test_spy_presenter_events_are_logged_in_render_audit(tmp_path):
    log_dir = tmp_path / "render"
    events_path = log_dir / "render-spy-event-test.jsonl"
    ansi_path = log_dir / "render-spy-event-test.ansi"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(events_path, ansi_path), theme="line")
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)
    try:
        presenter.emit("codex", SpyEvent(kind="context", text="raciocínio em progresso", transient=True))
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    spy_events = [e for e in events if e.get("event") == "spy_event"]
    assert spy_events
    assert spy_events[0].get("agent") == "codex"
    assert spy_events[0].get("kind") == "context"
    assert spy_events[0].get("transient") is True
    assert "raciocínio em progresso" in str(spy_events[0].get("preview", ""))
