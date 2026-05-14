import json
from unittest.mock import patch

from quimera.ui import RenderAuditLogger, TerminalRenderer


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_renderer_audit_logs_static_print_and_ansi(tmp_path):
    log_dir = tmp_path / "render"
    renderer = TerminalRenderer(audit_logger=RenderAuditLogger(log_dir), theme="line")
    try:
        renderer.show_system("System message")
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(log_dir / "render.jsonl")
    event_names = [event["event"] for event in events]
    assert "print" in event_names
    assert "noop" in event_names
    assert any("System message" in event.get("preview", "") for event in events)
    assert "System message" in (log_dir / "render.ansi").read_text(encoding="utf-8")


def test_renderer_audit_logs_stream_lifecycle(tmp_path):
    log_dir = tmp_path / "render"
    with patch("quimera.ui._is_interactive_terminal", return_value=True):
        renderer = TerminalRenderer(audit_logger=RenderAuditLogger(log_dir), theme="line")
        try:
            renderer.start_message_stream("codex")
            renderer.update_message_stream("codex", {"text": "partial"})
            renderer.finish_message_stream("codex", "final")
            renderer.flush()
        finally:
            renderer.close(timeout=1.0)

    events = _read_jsonl(log_dir / "render.jsonl")
    event_names = [event["event"] for event in events]
    assert "stream_start" in event_names
    assert "stream_chunk" in event_names
    assert "stream_stop" in event_names
    assert any(event.get("agent") == "codex" for event in events if event["event"].startswith("stream_"))
    assert (log_dir / "render.ansi").read_bytes()
