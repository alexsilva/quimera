import json
from unittest.mock import patch

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
