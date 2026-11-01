"""Linha estruturada de tool no feed Textual: casamento por id e renderização.

O modelo casa início e fim pela identidade da chamada (``tool_call_id``,
``request_id`` MCP HTTP), nunca pelo texto; o renderer monta a linha a partir
dos dados, na largura real.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rich.console import Console
from rich.text import Text

from quimera.domain.tool_line import build_tool_line
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.renderables import (
    _build_tools_renderable,
    _gutter_row,
    _render_event,
    _ToolLineRenderable,
)
from tests.textual_tool_helpers import tool_msg, tool_texts


def _model() -> tuple[TextualFeedModel, list[float]]:
    now = [100.0]
    return TextualFeedModel(clock=lambda: now[0]), now


def _send(model: TextualFeedModel, message: dict, agent: str = "claude", **payload) -> bool:
    return model.apply(TextualUiEvent("tool_preview", {**message, **payload}, agent=agent))


def _entries(model: TextualFeedModel, agent: str = "claude") -> list[dict]:
    return model._transient_tools_by_agent[agent]


def _render(renderable, width: int) -> list[str]:
    console = Console(record=True, width=width, color_system=None)
    console.print(renderable)
    return [line.rstrip() for line in console.export_text().splitlines()]


# --- casamento no modelo ------------------------------------------------------


def test_end_matched_by_id_updates_in_place_and_measures_missing_duration():
    model, now = _model()
    _send(model, tool_msg("Bash", {"command": "ls"}, tool_call_id="t1"))
    entry = _entries(model)[0]
    now[0] = 101.25

    _send(model, tool_msg("Bash", {"command": "ls"}, status="ok", tool_call_id="t1"))

    assert _entries(model) == [entry]
    assert _entries(model)[0] is entry
    assert entry["status"] == "ok"
    assert entry["duration_ms"] == 1250
    assert entry["text"] == "✓ ⌕ ls · 1.3s"


def test_identical_commands_with_distinct_ids_stay_on_two_lines():
    model, _ = _model()
    _send(model, tool_msg("Bash", {"command": "pytest -q"}, tool_call_id="t1"))
    _send(model, tool_msg("Bash", {"command": "pytest -q"}, tool_call_id="t2"))
    _send(model, tool_msg("Bash", {"command": "pytest -q"}, status="error", tool_call_id="t2", exit_code=1))

    assert [entry["status"] for entry in _entries(model)] == ["running", "error"]


def test_end_with_another_subject_keeps_the_subject_of_the_start():
    model, _ = _model()
    _send(model, tool_msg("read_file", {"path": "a.py"}, tool_call_id="t1"))
    _send(model, tool_msg("read_file", {"path": "outro.py"}, status="ok", tool_call_id="t1", duration_ms=3))

    (entry,) = _entries(model)
    assert entry["subject"] == "a.py"
    assert entry["text"] == "✓ ⌕ ler a.py · 3ms"


def test_end_without_id_matches_the_running_entry_of_same_tool_and_subject():
    model, _ = _model()
    _send(model, tool_msg("read_file", {"path": "a.py"}))
    _send(model, tool_msg("read_file", {"path": "b.py"}))
    _send(model, tool_msg("read_file", {"path": "a.py"}, status="ok", duration_ms=2))

    assert tool_texts(_entries(model)) == ["✓ ⌕ ler a.py · 2ms", "⌕ ler b.py"]


def test_generic_line_fuses_with_rich_line_and_maps_both_ids():
    model, _ = _model()
    _send(model, tool_msg("mcp__quimera__grep_search", tool_call_id="toolu_9"))
    _send(model, tool_msg("grep_search", {"pattern": "x", "path": "src"}, tool_call_id="exec-3"))

    (entry,) = _entries(model)
    assert entry["text"] == '⌕ buscar "x" em src'
    by_id = model._tool_call_line_by_agent["claude"]
    assert by_id["toolu_9"] is entry and by_id["exec-3"] is entry

    _send(model, tool_msg("grep_search", status="ok", tool_call_id="toolu_9", duration_ms=7))
    assert entry["text"] == '✓ ⌕ buscar "x" em src · 7ms'


def test_rich_line_never_fuses_into_a_finished_generic_line():
    model, _ = _model()
    _send(model, tool_msg("read_file", tool_call_id="toolu_1"))
    _send(model, tool_msg("read_file", status="ok", tool_call_id="toolu_1", duration_ms=2))
    _send(model, tool_msg("read_file", {"path": "b.py"}, tool_call_id="exec-2"))

    assert tool_texts(_entries(model)) == ["✓ ⌕ read_file · 2ms", "⌕ ler b.py"]


def test_second_end_for_the_same_id_is_ignored():
    model, _ = _model()
    _send(model, tool_msg("Bash", {"command": "make"}, tool_call_id="t1"))
    _send(model, tool_msg("Bash", {"command": "make"}, status="ok", tool_call_id="t1", duration_ms=10))
    _send(model, tool_msg("Bash", {"command": "make"}, status="error", tool_call_id="t1", duration_ms=99, tail="x"))

    (entry,) = _entries(model)
    assert (entry["status"], entry["duration_ms"], entry["tail"]) == ("ok", 10, "")


def test_cli_and_executor_ids_of_the_same_call_fuse_and_close_by_either_id():
    model, now = _model()
    _send(model, tool_msg("mcp__quimera__read_file", {"path": "a.py"}, tool_call_id="toolu_1"))
    _send(model, tool_msg("read_file", {"path": "a.py"}, tool_call_id="mcp:run1:5"))
    now[0] = 100.5
    _send(model, tool_msg("read_file", status="ok", tool_call_id="toolu_1", duration_ms=12))
    _send(model, tool_msg("read_file", status="error", tool_call_id="mcp:run1:5", duration_ms=99))

    (entry,) = _entries(model)
    assert (entry["status"], entry["duration_ms"]) == ("ok", 12)
    assert entry["text"] == "✓ ⌕ ler a.py · 12ms"
    by_id = model._tool_call_line_by_agent["claude"]
    assert by_id["toolu_1"] is entry and by_id["mcp:run1:5"] is entry


def test_cross_namespace_fusion_keeps_the_richer_subject():
    model, _ = _model()
    _send(model, tool_msg("mcp__quimera__tasks_list", {}, tool_call_id="toolu_1"))
    _send(model, tool_msg("tasks_list", {"limit": 50}, tool_call_id="mcp:run1:2"))
    _send(model, tool_msg("tasks_list", status="ok", tool_call_id="mcp:run1:2", duration_ms=4))

    (entry,) = _entries(model)
    assert entry["subject"] == "50"
    assert entry["text"] == "✓ ⌕ tasks_list 50 · 4ms"


def test_cross_namespace_fusion_of_two_rich_lines_keeps_the_richer_subject():
    model, _ = _model()
    _send(model, tool_msg("mcp__quimera__tasks_list", {"status": "open"}, tool_call_id="toolu_1"))
    _send(model, tool_msg("tasks_list", {"status": "open", "limit": 50}, tool_call_id="mcp:run1:2"))
    _send(model, tool_msg("tasks_list", status="ok", tool_call_id="toolu_1", duration_ms=4))

    (entry,) = _entries(model)
    assert entry["text"] == "✓ ⌕ tasks_list open 50 · 4ms"


def test_different_tools_never_fuse_across_namespaces():
    model, _ = _model()
    _send(model, tool_msg("Bash", {"command": "ls", "description": "Listar"}, tool_call_id="toolu_1"))
    _send(model, tool_msg("exec_command", {"cmd": "ls -la"}, tool_call_id="mcp:run1:3"))

    # Tools diferentes nunca se fundem, mesmo de namespaces distintos.
    assert len(_entries(model)) == 2


def test_two_cli_calls_of_the_same_tool_never_fuse():
    model, _ = _model()
    _send(model, tool_msg("read_file", {"path": "a.py"}, tool_call_id="toolu_1"))
    _send(model, tool_msg("read_file", {"path": "a.py"}, tool_call_id="toolu_2"))

    assert len(_entries(model)) == 2


def test_executor_line_before_cli_line_also_fuses():
    model, _ = _model()
    _send(model, tool_msg("grep_search", {"pattern": "x", "path": "."}, tool_call_id="mcp:run1:6"))
    _send(model, tool_msg("mcp__quimera__grep_search", {"pattern": "x"}, tool_call_id="toolu_2"))
    _send(model, tool_msg("grep_search", status="ok", tool_call_id="toolu_2", duration_ms=9))

    (entry,) = _entries(model)
    assert entry["text"] == '✓ ⌕ buscar "x" em . · 9ms'


def test_ambiguous_cross_namespace_candidates_with_other_subjects_stay_apart():
    model, _ = _model()
    _send(model, tool_msg("read_file", {"path": "a.py"}, tool_call_id="toolu_1"))
    _send(model, tool_msg("read_file", {"path": "b.py"}, tool_call_id="toolu_2"))
    _send(model, tool_msg("read_file", {"path": "c.py"}, tool_call_id="mcp:run1:9"))

    assert len(_entries(model)) == 3


def test_delegation_consumes_cli_and_executor_lines_fused_by_namespace():
    model, _ = _model()
    model.apply(TextualUiEvent("agent_update", {"content": "delegando", "run_id": "r1"}, agent="claude"))
    request = {"target_agent": "codex", "request": "revisar"}
    _send(model, tool_msg("mcp__quimera__tasks_delegate", request, tool_call_id="toolu_9"), run_id="r1")
    _send(model, tool_msg("tasks_delegate", request, tool_call_id="mcp:run1:7"), run_id="r1")
    assert len(_entries(model, "claude#run:r1")) == 1

    model.apply(TextualUiEvent(
        "delegation",
        {"from_agent": "claude", "to_agent": "codex", "task": "revisar", "delegation_id": "d1",
         "tool_call_id": "mcp:run1:7", "source_run_id": "r1"},
    ))

    assert "tools" not in model.items[0].event.payload
    assert model._tool_call_line_by_agent["claude#run:r1"] == {}


def test_bool_duration_from_mcp_http_state_is_not_a_duration():
    model, _ = _model()
    _send(model, tool_msg("read_file", {"path": "a.py"}, transport="mcp_http"), transport="mcp_http",
          mcp_msg_id="1", run_id="h1")
    model.apply(TextualUiEvent(
        "tool_state",
        {"transport": "mcp_http", "status": "finished", "tool_name": "read_file", "mcp_msg_id": "1",
         "run_id": "h1", "duration_ms": True},
        agent="claude",
    ))

    (entry,) = _entries(model)
    assert entry["duration_ms"] == 0
    assert "True" not in entry["text"]


def test_diff_preview_for_an_id_is_attached_and_capped():
    model, _ = _model()
    _send(model, tool_msg("Edit", {"file_path": "a.py"}, tool_call_id="e1"))
    _send(model, {"content": "@@ -1 +1 @@\n-velho\n+novo", "preview_for": "e1"})
    for index in range(30):
        _send(model, {"content": f"+linha {index}", "preview_for": "e1"})

    (entry,) = _entries(model)
    assert entry["preview_lines"][:3] == ["@@ -1 +1 @@", "-velho", "+novo"]
    assert len(entry["preview_lines"]) == 20


def test_diff_preview_without_matching_entry_becomes_a_legacy_line():
    model, _ = _model()
    _send(model, {"content": "+solta", "preview_for": "sem-dono"})

    (entry,) = _entries(model)
    assert entry["tool"] == "" and entry["text"] == "+solta"


def test_legacy_lines_never_match_structured_ones():
    model, _ = _model()
    _send(model, {"content": "✓ ⌕ ler a.py", "tool_call_id": "t1"})
    _send(model, tool_msg("read_file", {"path": "a.py"}, status="ok", tool_call_id="t1", duration_ms=1))

    assert [entry["tool"] for entry in _entries(model)] == ["", "read_file"]


def test_mcp_http_tool_state_applies_outcome_to_the_request_entry():
    model, now = _model()
    http = {"transport": "mcp_http", "client_name": "chatgpt", "run_id": "h1"}
    model.apply(TextualUiEvent(
        "tool_preview",
        {**http, "mcp_msg_id": "1", **tool_msg("read_file", {"path": "a.py"})},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_preview",
        {**http, "mcp_msg_id": "2", **tool_msg("read_file", {"path": "a.py"})},
        agent="mcp-http",
    ))
    now[0] = 100.5
    model.apply(TextualUiEvent(
        "tool_state",
        {**http, "msg_id": "2", "tool_name": "read_file", "status": "cancelled"},
        agent="mcp-http",
    ))
    model.apply(TextualUiEvent(
        "tool_state",
        {**http, "msg_id": "9", "tool_name": "git_status", "status": "finished", "duration_ms": 4},
        agent="mcp-http",
    ))

    tools = model.items[0].event.payload["tools"]
    assert tool_texts(tools) == [
        "◇ ⌕ ler a.py",
        "◇ ✗ ⌕ ler a.py · 500ms",
        "◇ ✓ ⌕ git status · 4ms",
    ]
    assert all(entry["transport"] == "mcp_http" for entry in tools)


def test_live_block_overflow_line_is_a_marker_dict():
    model, _ = _model()
    for index in range(7):
        _send(model, tool_msg("Bash", {"command": f"echo {index}"}, tool_call_id=f"t{index}"))

    tools = model.items[0].event.payload["tools"]
    assert tools[0] == {"marker": True, "text": "⋮ +2 ferramentas anteriores"}
    assert len(tools) == 6
    # O bloco recebe cópias: o payload já emitido não muda com o próximo fim.
    assert tools[1] is not _entries(model)[2]


def test_delegation_consumes_the_fused_entry_and_all_its_ids():
    model, _ = _model()
    model.apply(TextualUiEvent("agent_update", {"content": "delegando", "run_id": "r1"}, agent="claude"))
    _send(model, tool_msg("Bash", {"command": "ls"}, tool_call_id="t0"), run_id="r1")
    _send(model, tool_msg("mcp__quimera__tasks_delegate", tool_call_id="toolu_1"), run_id="r1")
    _send(
        model,
        tool_msg("tasks_delegate", {"target_agent": "codex", "request": "revisar"}, tool_call_id="exec-1"),
        run_id="r1",
    )

    model.apply(TextualUiEvent(
        "delegation",
        {"from_agent": "claude", "to_agent": "codex", "task": "revisar", "delegation_id": "d1",
         "tool_call_id": "exec-1", "source_run_id": "r1"},
    ))

    assert tool_texts(model.items[0].event.payload["tools"]) == ["⌕ ls"]
    assert set(model._tool_call_line_by_agent["claude#run:r1"]) == {"t0"}


def test_final_keeps_structured_tool_lines_for_the_toggle():
    model, _ = _model()
    model.apply(TextualUiEvent("agent_update", {"content": "x", "run_id": "r1"}, agent="claude"))
    _send(model, tool_msg("read_file", {"path": "a.py"}, status="ok", tool_call_id="t1", duration_ms=1), run_id="r1")
    model.apply(TextualUiEvent("agent_message", {"content": "pronto", "run_id": "r1"}, agent="claude"))

    (line,) = model.items[0].event.payload["tool_lines"]
    assert line["tool"] == "read_file" and line["status"] == "ok"


# --- renderização ---------------------------------------------------------------


def _payload(*args, **kwargs) -> dict:
    return build_tool_line(*args, **kwargs).to_payload()


def test_compact_line_ellipsizes_body_in_the_middle_and_keeps_duration():
    path = "quimera/ui/textual/very/long/directory/structure/that/goes/on/renderables_module.py"
    entry = _payload("read_file", {"path": path}, status="ok", duration_ms=300)

    (line,) = _render(_ToolLineRenderable(entry, "cyan", compact=True), 56)

    assert len(line) <= 56
    assert line.startswith("✓ ⌕ ler quimera/ui/")
    assert line.endswith("renderables_module.py · 300ms")
    assert "…" in line


def test_compact_line_cuts_the_tail_from_the_end_only_after_the_body():
    entry = _payload(
        "Bash", {"command": "pytest -q tests/test_textual_ui.py"},
        status="error", duration_ms=12400, tail="2 failed, 283 passed, 4 warnings in 117.55s (0:01:57)",
    )

    (line,) = _render(_ToolLineRenderable(entry, "cyan", compact=True), 56)

    assert len(line) <= 56
    assert line.startswith("✗ ⚙ ")
    assert " · 12.4s · 2 failed" in line
    assert line.endswith("…")


def test_status_and_category_glyph_styles():
    ok = _ToolLineRenderable(_payload("read_file", {"path": "a"}, status="ok"), "blue", compact=True)._prefix()
    err = _ToolLineRenderable(_payload("read_file", {"path": "a"}, status="error"), "blue", compact=True)._prefix()
    running = _ToolLineRenderable(
        _payload("git_status", transport="mcp_http"), "blue", compact=True
    )._prefix()

    def styles(text: Text) -> list[tuple[str, str]]:
        return [(text.plain[span.start:span.end], str(span.style)) for span in text.spans]

    assert styles(ok) == [("✓ ", "bold green"), ("⌕ ", "bold blue")]
    assert styles(err) == [("✗ ", "bold red"), ("⌕ ", "bold blue")]
    assert styles(running) == [("◇ ", "dim"), ("· ", "dim"), ("⌕ ", "bold blue")]


def test_expanded_line_shows_command_title_tail_and_colored_diff_with_hanging_indent():
    entry = _payload(
        "Bash",
        {"command": "grep -n tool quimera/ui/textual/renderables.py | head -80 && wc -l x.py",
         "description": "Locate the tool rendering code in the feed renderables module"},
        status="error", duration_ms=300, tail="grep: x.py: No such file",
        preview_lines=["@@ -1 +1 @@", "-velho", "+novo", " contexto"],
    )
    renderable = _ToolLineRenderable(entry, "cyan", compact=False)

    lines = _render(renderable, 40)

    # O comando é o corpo; a descrição é complemento abaixo, quebrada por palavra.
    assert lines[0] == "✗ ⌕ grep -n tool quimera/ui/textual/rend"
    # Continuação dobrada na coluna do corpo, nunca na do glifo.
    assert lines[1] == "    erables.py | head -80 && wc -l x.py"
    assert lines[2] == "    · 300ms"
    assert lines[3:5] == ["    Locate the tool rendering code in", "    the feed renderables module"]
    assert "    grep: x.py: No such file" in lines
    assert lines[-4:] == ["    @@ -1 +1 @@", "    -velho", "    +novo", "     contexto"]

    console = Console(record=True, width=40)
    segments = list(console.render(renderable, console.options.update_width(40)))
    colors = {segment.text: str(segment.style) for segment in segments if segment.text.strip()}
    assert colors["+novo"] == "green"
    assert colors["-velho"] == "red"
    assert colors["@@ -1 +1 @@"] == "cyan"
    assert colors["grep: x.py: No such file"] == "red"


def test_expanded_line_keeps_the_duration_token_whole_at_the_end():
    entry = _payload(
        "tasks_delegate",
        {"target_agent": "claude-opus", "request": "Corrigir bloqueadores da revisão do feed hoje"},
        status="ok", duration_ms=420000,
    )

    lines = _render(_ToolLineRenderable(entry, "magenta", compact=False), 40)

    assert all(len(line) <= 40 for line in lines)
    assert sum("7m00s" in line for line in lines) == 1
    assert lines[-1].endswith("· 7m00s")
    assert not any(line.rstrip().endswith("7m00") for line in lines)


def test_expanded_duration_goes_to_its_own_line_when_the_last_one_is_full():
    entry = _payload("read_file", {"path": "a" * 66}, status="ok", duration_ms=420000)

    lines = _render(_ToolLineRenderable(entry, "cyan", compact=False), 40)

    assert lines[-1] == "    · 7m00s"
    assert all(len(line) <= 40 for line in lines)


def test_bool_duration_is_not_rendered():
    entry = {**_payload("read_file", {"path": "a.py"}, status="ok"), "duration_ms": True}

    for compact in (True, False):
        lines = _render(_ToolLineRenderable(entry, "cyan", compact=compact), 40)
        assert lines[0] == "✓ ⌕ ler a.py"


def test_gutter_row_hanging_indents_continuation_lines():
    text = Text("aaaa bbbb cccc dddd eeee", no_wrap=False)

    lines = _render(_gutter_row("│", "", text, continuation="│", hanging=2), 3 + 11)

    assert lines == ["│  aaaa bbbb", "│    cccc dddd", "│    eeee"]


def test_legacy_entry_renders_raw_text_without_glyphs():
    lines = _render(_build_tools_renderable([{"tool": "", "text": "⚒ algo cru"}], "cyan"), 40)

    assert lines == ["│  ⚒ algo cru"]


def test_final_expanded_renders_full_entries_and_collapsed_hides_them():
    entry = _payload(
        "Bash", {"command": "pytest -q", "description": "Roda os testes"}, status="ok", duration_ms=300
    )
    payload = {"content": "pronto", "tool_lines": [entry], "feed_seq": 1, "label": "Claude"}

    collapsed = "\n".join(_render(_render_event(TextualUiEvent("agent_message", payload, agent="claude")), 60))
    expanded = "\n".join(_render(
        _render_event(TextualUiEvent("agent_message", {**payload, "tools_expanded": True}, agent="claude")), 60
    ))

    assert "pytest -q" not in collapsed
    assert "✓ ⚙ pytest -q · 300ms" in expanded
    assert "    Roda os testes" in expanded


# --- evidência visual (Pilot, 80x24) ---------------------------------------------


_LONG_PATH = "quimera/ui/textual/very/long/directory/structure/for/the/feed/renderables_module.py"


def emit_tool_line_scenario(bridge) -> None:
    """Bloco vivo com as cinco formas de linha; usado também no "antes"."""
    def update(content: str) -> None:
        bridge.emit(TextualUiEvent("agent_update", {"content": content, "run_id": "r1"}, agent="claude"))

    def preview(message: dict) -> None:
        bridge.emit(TextualUiEvent("tool_preview", {**message, "run_id": "r1"}, agent="claude"))

    update("[thinking] investigando o feed")
    grep = {"command": "grep -n tool quimera/ui/textual/renderables.py | head -80",
            "description": "Locate tool rendering code"}
    preview(tool_msg("Bash", grep, tool_call_id="toolu_1"))
    preview(tool_msg("Bash", grep, status="ok", tool_call_id="toolu_1", duration_ms=300))
    pytest_args = {"command": "pytest -q tests/test_textual_ui.py"}
    preview(tool_msg("Bash", pytest_args, tool_call_id="toolu_2"))
    preview(tool_msg("Bash", pytest_args, status="error", tool_call_id="toolu_2",
                     duration_ms=12400, tail="2 failed", exit_code=1))
    preview(tool_msg("read_file", {"path": _LONG_PATH}, tool_call_id="toolu_3"))
    preview(tool_msg("tasks_delegate", {"target_agent": "codex", "request": "Revise o módulo de renderables"},
                     tool_call_id="toolu_4"))
    preview(tool_msg("git_status", status="ok", duration_ms=40, transport="mcp_http", tool_call_id="exec-5"))


async def capture_tool_line_screens(app, bridge, pilot) -> tuple[str, str]:
    from quimera.ui.textual.screenshot import capture_textual_screenshot

    await pilot.pause()
    emit_tool_line_scenario(bridge)
    await pilot.pause()
    await pilot.pause()
    live = capture_textual_screenshot(app).text
    bridge.emit(TextualUiEvent("agent_message", {"content": "pronto", "run_id": "r1"}, agent="claude"))
    await pilot.pause()
    await pilot.press("ctrl+o")
    await pilot.pause()
    await pilot.pause()
    final = capture_textual_screenshot(app).text
    return live, final


def test_tool_lines_visual_evidence_80x24():
    from tests.test_textual_app_behavior import _app

    app, bridge = _app()
    out_dir = Path(os.environ.get("QPT_EVIDENCE_DIR", "/tmp/qpt/d3"))
    out_dir.mkdir(parents=True, exist_ok=True)

    async def run() -> tuple[str, str]:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.resize_terminal(80, 24)
            return await capture_tool_line_screens(app, bridge, pilot)

    live, final = asyncio.run(run())
    (out_dir / "tool_lines_after_live.txt").write_text(live + "\n", encoding="utf-8")
    (out_dir / "tool_lines_after_final.txt").write_text(final + "\n", encoding="utf-8")

    assert "✓ ⌕ grep -n tool quimera/ui/textual/renderables.py | head -80 · 300ms" in live
    assert "Locate tool rendering code" not in live
    assert "✗ ⚙ pytest -q tests/test_textual_ui.py · 12.4s · 2 failed" in live
    assert "renderables_module.py" in live and "…" in live
    assert "· ⇄ delegar codex · Revise o módulo de renderables" in live
    assert "◇ ✓ ⌕ git status · 40ms" in live
    assert "grep -n tool quimera/ui/textual/renderables.py | head -80" in final
    assert "very/long/directory" in final
