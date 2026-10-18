"""`RenderAuditLogger`: eventos estruturados de render por sessão (``--debug``)."""
import json

from rich.console import Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from quimera.agent_events import SpyEvent
from quimera.constants import Visibility
from quimera.spy_output_presenter import SpyOutputPresenter
from quimera.ui import PlainTextRenderer, RenderAuditLogger


def _read_jsonl(path):
    """Lê arquivo JSONL e retorna lista de dicionários."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _plain_renderer(events_path):
    import io

    return PlainTextRenderer(stream=io.StringIO(), audit_logger=RenderAuditLogger(events_path))


def test_audit_logger_records_events_only(tmp_path):
    """Só o jsonl existe: nenhum stream ANSI é criado ao lado."""
    events_path = tmp_path / "render" / "render-sessao.jsonl"
    with RenderAuditLogger(events_path) as logger:
        logger.log_event("tui_resize", width=80, height=24, path=tmp_path / "x.svg", extra={"a": (1, 2)})

    events = _read_jsonl(events_path)
    assert [event["event"] for event in events] == ["tui_resize"]
    assert events[0]["width"] == 80
    assert events[0]["path"] == str(tmp_path / "x.svg")
    assert events[0]["extra"] == {"a": [1, 2]}
    assert "ts" in events[0]
    assert list((tmp_path / "render").iterdir()) == [events_path]


def test_audit_logger_ignores_events_after_close(tmp_path):
    events_path = tmp_path / "render" / "render-sessao.jsonl"
    logger = RenderAuditLogger(events_path)
    logger.log_event("print", preview="antes")
    logger.close()
    logger.close()  # idempotente
    logger.log_event("print", preview="depois")

    assert [event["preview"] for event in _read_jsonl(events_path)] == ["antes"]


def test_renderer_audit_logs_print_with_preview(tmp_path):
    """O renderer de texto puro grava um evento print por escrita."""
    events_path = tmp_path / "render" / "render-test-session.jsonl"
    renderer = _plain_renderer(events_path)
    try:
        renderer.show_system("System message")
        renderer.flush()
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    assert [event["event"] for event in events] == ["print"]
    assert events[0]["kind"] == "system"
    assert "System message" in events[0]["preview"]


def test_renderer_show_approval_logs_distinct_preview(tmp_path):
    """Verifica que approval usa evento/texto próprio no renderer."""
    events_path = tmp_path / "render" / "render-approval-test.jsonl"
    renderer = _plain_renderer(events_path)
    try:
        renderer.show_approval("Aprovar exec_command\ncmd=rm -rf /tmp/demo")
    finally:
        renderer.close(timeout=1.0)

    events = _read_jsonl(events_path)
    assert [event["kind"] for event in events] == ["approval"]
    assert events[0]["preview"].startswith("⚠ Aprovar exec_command")
    assert "cmd=rm -rf /tmp/demo" in events[0]["preview"]


def test_audit_preview_extracts_text_from_rich_group(tmp_path):
    """Previews extraem texto legível de objetos Rich, sem repr."""
    events_path = tmp_path / "render" / "render-group-test.jsonl"
    renderer = _plain_renderer(events_path)
    try:
        renderer.show_plain(Group(Text("Hello"), Text("World")))
    finally:
        renderer.close(timeout=1.0)

    print_events = [e for e in _read_jsonl(events_path) if e["event"] == "print"]
    assert len(print_events) == 1
    preview = print_events[0]["preview"]
    assert "Hello" in preview
    assert "World" in preview
    assert "<rich.console.Group" not in preview
    assert "object at" not in preview


def test_audit_preview_extracts_text_from_table_and_rule_group(tmp_path):
    events_path = tmp_path / "render" / "render-table-rule-test.jsonl"
    renderer = _plain_renderer(events_path)
    try:
        table = Table()
        table.add_column("Tool")
        table.add_column("Status")
        table.add_row("exec", "ok")
        renderer.show_plain(Group(table, Rule("Prompt Preview")))
    finally:
        renderer.close(timeout=1.0)

    print_events = [e for e in _read_jsonl(events_path) if e["event"] == "print"]
    assert len(print_events) == 1
    preview = print_events[0]["preview"]
    for token in ("Tool", "Status", "exec", "ok", "Prompt Preview"):
        assert token in preview
    assert "object at" not in preview


def test_audit_no_empty_print_events(tmp_path):
    """Prints vazios não geram entradas de audit."""
    events_path = tmp_path / "render" / "render-empty-test.jsonl"
    renderer = _plain_renderer(events_path)
    try:
        renderer.show_plain("")
        renderer.show_system("   ")
    finally:
        renderer.close(timeout=1.0)

    assert _read_jsonl(events_path) == []


def test_spy_presenter_events_are_logged_in_render_audit(tmp_path):
    """Eventos do SpyOutputPresenter chegam ao audit via log_debug_event."""
    events_path = tmp_path / "render" / "render-spy-event-test.jsonl"
    renderer = _plain_renderer(events_path)
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)
    try:
        presenter.emit("codex", SpyEvent(kind="context", text="raciocínio em progresso", transient=True))
    finally:
        renderer.close(timeout=1.0)

    spy_events = [e for e in _read_jsonl(events_path) if e.get("event") == "spy_event"]
    assert spy_events
    assert spy_events[0]["agent"] == "codex"
    assert spy_events[0]["kind"] == "context"
    assert spy_events[0]["transient"] is True
    assert "raciocínio em progresso" in str(spy_events[0]["preview"])
