"""`PlainTextRenderer`: renderer de texto puro do modo pipe e do --interactive-test."""
import io
import json
import threading
from unittest.mock import patch

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from quimera.ui import PlainTextRenderer, RenderAuditLogger, RendererBase


def _make():
    stream = io.StringIO()
    return PlainTextRenderer(stream=stream), stream


def test_is_a_renderer_without_structured_channels():
    assert issubclass(PlainTextRenderer, RendererBase)
    assert PlainTextRenderer.supports_agent_feed is False
    assert PlainTextRenderer.supports_structured_agent_activity is False
    assert PlainTextRenderer.supports_screenshot is False


def test_writes_messages_in_order_as_plain_lines():
    renderer, stream = _make()

    renderer.show_system("iniciando")
    renderer.show_message("codex", "resposta curta")
    renderer.show_plain("linha solta", agent="codex", muted=True)
    renderer.show_feed("saída de tool")

    assert stream.getvalue() == "iniciando\n🤖  Codex: resposta curta\nlinha solta\nsaída de tool\n"


def test_multiline_agent_message_puts_the_label_on_its_own_line():
    renderer, stream = _make()

    renderer.show_message("codex", "primeira\nsegunda")

    assert stream.getvalue() == "🤖  Codex:\nprimeira\nsegunda\n"


def test_strips_ansi_and_extracts_text_from_rich_renderables():
    renderer, stream = _make()

    renderer.show_message("codex", "\x1b[31mvermelho\x1b[0m")
    renderer.show_plain(Panel(Text("conteúdo interno"), title="Título"))
    renderer.show_system(Group(Text("a"), Text("b")))

    output = stream.getvalue()
    assert "\x1b[" not in output
    assert "🤖  Codex: vermelho\n" in output
    assert "Título conteúdo interno\n" in output
    assert "a b\n" in output
    assert "rich." not in output


def test_warnings_errors_and_approvals_are_marked():
    renderer, stream = _make()

    renderer.show_warning("cuidado")
    renderer.show_error("falhou", agent="codex")
    renderer.show_approval("Aprovar exec_command\ncmd=rm -rf /tmp/demo")

    assert stream.getvalue() == "⚠ cuidado\n✖ falhou\n⚠ Aprovar exec_command\ncmd=rm -rf /tmp/demo\n"


def test_structured_agent_errors_keep_their_message():
    """AgentClient manda mensagem vazia com error_kind; o texto vem do kind."""
    renderer, stream = _make()

    renderer.show_error("", agent="codex", error_kind="agent_invalid_output")
    renderer.show_error("", command_name="codex-cli", error_kind="agent_exit", return_code=3)
    renderer.show_error("timeout", agent="codex", error_kind="agent_comm")
    renderer.show_error("texto livre", agent="codex")

    assert stream.getvalue() == (
        "✖ [erro] agente codex não retornou saída válida\n"
        "✖ [erro] agente codex-cli retornou código 3\n"
        "✖ [erro] falha ao comunicar com codex: timeout\n"
        "✖ texto livre\n"
    )


def test_transients_and_statuses_are_not_written():
    renderer, stream = _make()

    renderer.update_agent_transient("codex", "pensando…", run_id="run:1")
    renderer.clear_agent_transient("codex", run_id="run:1")
    with renderer.running_status("conectando", agent="codex") as status:
        assert status is None
    renderer.begin_agent_run("codex", run_id="run:1")
    renderer.end_agent_run("codex", run_id="run:1", status="finished")
    renderer.show_prompt_preview("codex", "preview")

    assert stream.getvalue() == ""


def test_empty_messages_are_skipped():
    renderer, stream = _make()

    renderer.show_system("")
    renderer.show_plain("   \n")
    renderer.show_message("codex", None)

    assert stream.getvalue() == "🤖  Codex: \n"


def test_delegation_no_response_and_text_window_lines():
    renderer, stream = _make()

    renderer.show_delegation("claude", "codex", "revise o patch", delegation_id="d1")
    renderer.show_no_response("codex")
    renderer.show_text_window("Contexto", "conteúdo longo")
    renderer.show_turn_summary("codex", {"tools": [{"status": "ok", "duration_ms": 1200}]})

    assert stream.getvalue() == (
        "delegação 🤖  Claude → 🤖  Codex: revise o patch\n"
        "🤖  Codex: sem resposta válida\n"
        "── Contexto ──\nconteúdo longo\n"
        "TOOLS: 1 chamadas · 1 ok · 0 erro · 1.2s\n"
    )


def test_profile_resolver_drives_agent_labels():
    renderer, stream = _make()
    renderer.set_profile_resolver(lambda name: ("magenta", "🔮  Claude") if name == "claude" else None)

    renderer.show_message("claude", "oi")
    renderer.show_message("outro", "oi")

    assert stream.getvalue() == "🔮  Claude: oi\n🤖  Outro: oi\n"


def test_default_stream_is_stdout_resolved_at_write_time():
    renderer = PlainTextRenderer()
    captured = io.StringIO()

    with patch("sys.stdout", captured):
        renderer.print_direct("direto")
        renderer.show_system("sistema")

    assert captured.getvalue() == "direto\nsistema\n"


def test_concurrent_writes_keep_each_message_whole():
    renderer, stream = _make()
    lines = [f"mensagem {index} " + "x" * 200 for index in range(50)]

    def worker(start: int) -> None:
        for line in lines[start::4]:
            renderer.show_plain(line)

    threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(stream.getvalue().splitlines()) == sorted(lines)


def test_theme_is_tracked_for_persistence_without_styling_output():
    renderer, stream = _make()

    assert renderer.theme_name == "chat"
    assert renderer.set_theme("rule") == "rule"
    assert renderer.cycle_theme() == "minimal"
    assert renderer.theme_name == "minimal"
    assert PlainTextRenderer(stream=stream, theme="inexistente").theme_name == "chat"
    renderer.show_message("codex", "oi")
    assert stream.getvalue() == "🤖  Codex: oi\n"


def test_audit_logger_receives_one_print_event_per_write(tmp_path):
    events_path = tmp_path / "render" / "render-plain.jsonl"
    renderer, _stream = _make()
    renderer.set_audit_logger(RenderAuditLogger(events_path))

    renderer.show_system("System message")
    renderer.show_message("codex", "resposta")
    renderer.update_agent_transient("codex", "ignorado")
    renderer.log_debug_event("spy_event", agent="codex", kind="context")
    renderer.close()

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [event["event"] for event in events] == ["print", "print", "spy_event"]
    assert events[0]["kind"] == "system"
    assert events[0]["preview"] == "System message"
    assert events[1]["kind"] == "agent_message"
    assert events[1]["agent"] == "codex"
    assert "resposta" in events[1]["preview"]
    assert renderer.audit_logger is None
    # Fechado: novas escritas vão para o stream, mas não para o audit.
    renderer.show_system("depois")
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 3


def test_close_and_flush_tolerate_missing_audit_logger():
    renderer, stream = _make()

    assert renderer.flush() is None
    assert renderer.close() is None
    renderer.show_system("ainda escreve")

    assert stream.getvalue() == "ainda escreve\n"
