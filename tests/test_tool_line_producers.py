"""Produtores da linha estruturada de tool: perfis, presenter e cliente."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quimera.agent_events import SpyEvent
from quimera.agents import AgentClient
from quimera.domain.task_states import Visibility
from quimera.domain.tool_line import build_tool_line
from quimera.profiles.claude import _format_claude_spy_event
from quimera.profiles.codex import _format_codex_spy_event
from quimera.profiles.opencode import _format_opencode_spy_event
from quimera.spy_output_presenter import SpyOutputPresenter


def _feed_renderer() -> MagicMock:
    renderer = MagicMock()
    renderer.supports_agent_feed = True
    return renderer


def _tool_use(name, tool_input, tool_id):
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]},
    })


def _tool_result(tool_id, content, is_error=False):
    return json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
        ]},
    })


# --- claude -----------------------------------------------------------------

def test_claude_tool_use_carries_id_description_and_line():
    [event] = _format_claude_spy_event(
        _tool_use("Bash", {"command": "git status", "description": "Mostra o estado"}, "toolu_1")
    )

    assert event.text == "⌕ Mostra o estado"
    assert event.data["tool_call_id"] == "toolu_1"
    assert event.data["operation"] == "start"
    assert event.data["line"]["title"] == "Mostra o estado"
    assert event.data["line"]["tool_call_id"] == "toolu_1"


@pytest.mark.parametrize(
    ("content", "is_error", "status", "tail"),
    [
        ("linha 1\n\nlinha final\n", False, "ok", "linha final"),
        ([{"type": "text", "text": "falhou\nexit 2"}], True, "error", "exit 2"),
        ("", False, "ok", ""),
    ],
)
def test_claude_user_tool_result_closes_the_tool(content, is_error, status, tail):
    [event] = _format_claude_spy_event(_tool_result("toolu_9", content, is_error))

    assert event.kind == "tool"
    assert event.data["operation"] == "end"
    assert event.data["tool_call_id"] == "toolu_9"
    assert event.data["status"] == status
    assert event.data["tail"] == tail


def test_claude_user_message_without_tool_result_is_ignored():
    line = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "oi"}]}})

    assert _format_claude_spy_event(line) == []


def test_presenter_pairs_claude_start_and_end_by_id():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.consume_stdout("claude", _tool_use("Read", {"file_path": "README.md"}, "toolu_7"))
    presenter.consume_stdout("claude", _tool_result("toolu_7", "ok"))

    start, end = (call.args[0] for call in renderer.show_tool_preview.call_args_list)
    assert start["tool_call_id"] == end["tool_call_id"] == "toolu_7"
    assert start["tool_line"]["status"] == "running"
    assert end["tool_line"]["status"] == "ok"
    assert end["tool_line"]["tool"] == "read"
    assert end["tool_line"]["verb"] == "ler"
    assert end["tool_line"]["subject"] == "README.md"
    assert isinstance(end["tool_line"]["duration_ms"], int)
    assert end["content"].startswith("✓ ⌕ ler README.md")
    detail = presenter.finalize_turn("claude")
    assert detail["tools"][0]["status"] == "ok"


@pytest.mark.parametrize("is_error", [False, True])
def test_claude_native_tool_end_activity_resolves_tool_and_text_from_the_start(is_error):
    from quimera.domain.agent_run_events import AgentRunEvent, AgentRunRegistry

    client = AgentClient(_feed_renderer(), visibility=Visibility.SUMMARY)
    activities: list[dict] = []
    registry = AgentRunRegistry()
    for raw in (
        _tool_use("Bash", {"command": "pytest -q", "description": "Rodar testes"}, "toolu_1"),
        _tool_result("toolu_1", "128 passed", is_error),
    ):
        client._notify_cli_run_activities("claude", raw, activities.append)
        client._spy_output_presenter.consume_stdout("claude", raw)
    for activity in activities:
        # Mesmo texto que ``agent_gateway._on_run_activity`` publica.
        registry.record(AgentRunEvent(
            "activity", agent="claude", run_id="r1",
            text=str(activity.get("text") or activity.get("tool") or ""),
        ))

    start, end = activities
    assert (start["tool"], end["tool"]) == ("Bash", "Bash")
    mark = "✗" if is_error else "✓"
    assert end["text"] == f"{mark} ⚙ Rodar testes · 128 passed"
    assert end["tool_call_id"] == "toolu_1"
    run = registry.get("r1")
    assert run is not None and run.last_activity == end["text"]


@pytest.mark.parametrize(
    "text",
    [
        "tool: exec_command",
        build_tool_line("exec_command", {"cmd": "ls"}).to_text(),
        build_tool_line("read_file", {"path": "a.py"}).to_text(),
        build_tool_line("read_file", {"path": "a.py"}, status="ok").to_text(),
        build_tool_line("Bash", {"command": "make"}, status="error", exit_code=2).to_text(),
        build_tool_line("read_file", {"path": "a.py"}, transport="mcp_http").to_text(),
    ],
)
def test_client_recognizes_canonical_tool_line_prefixes(text):
    assert AgentClient._is_tool_call_text(f"  {text}\n")


@pytest.mark.parametrize("text", ["Traceback (most recent call last):", "warning: x", "$HOME não definido", "✓feito"])
def test_client_does_not_take_plain_stderr_for_a_tool_line(text):
    assert not AgentClient._is_tool_call_text(text)


def test_client_mutes_canonical_tool_line_on_stderr_and_keeps_other_lines_plain():
    from unittest.mock import patch

    renderer = MagicMock()
    renderer.supports_agent_feed = False
    client = AgentClient(renderer, visibility=Visibility.FULL)
    tool_text = build_tool_line("read_file", {"path": "a.py"}).to_text()
    with patch("subprocess.Popen") as mock_popen:
        proc = MagicMock()
        proc.stdout = iter(['{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"ok"}}\n'])
        proc.stderr = iter([f"{tool_text}\n", "aviso comum\n"])
        proc.returncode = 0
        proc.stdin = MagicMock()
        mock_popen.return_value = proc
        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent=None, show_status=False)

    renderer.show_plain.assert_any_call(tool_text, agent=None, muted=True)
    renderer.show_plain.assert_any_call("aviso comum", agent=None)


# --- codex ------------------------------------------------------------------

def _codex_command(phase, command, **item):
    return _format_codex_spy_event(json.dumps({
        "type": f"item.{phase}",
        "item": {"type": "command_execution", "command": command, **item},
    }))


def test_codex_command_subject_drops_shell_wrapper():
    [start] = _codex_command("started", "/bin/bash -lc 'git status --short'", id="c1")

    assert start.data["line"]["subject"] == "git status --short"
    assert start.data["input"] == {"cmd": "/bin/bash -lc 'git status --short'"}
    assert "bash" not in start.text


def test_codex_failed_command_reports_exit_code():
    [end] = _codex_command("completed", "pytest -q", id="c2", exit_code=1)

    assert end.text.endswith("· exit 1")
    assert end.text.startswith("✗ ")
    assert end.data["status"] == "error"
    assert end.data["line"]["tail"] == "exit 1"


def test_codex_aggregated_output_becomes_tail():
    [end] = _codex_command(
        "completed", "pytest -q", id="c3", exit_code=1, aggregated_output="coleta\n\n1 failed in 0.1s\n"
    )

    assert end.data["line"]["tail"] == "1 failed in 0.1s"
    assert end.text.endswith("· 1 failed in 0.1s")


def test_codex_mcp_tool_call_start_and_end():
    started = json.dumps({
        "type": "item.started",
        "item": {"id": "m1", "type": "mcp_tool_call", "server": "quimera", "tool": "read_file",
                 "arguments": json.dumps({"path": "README.md"}), "status": "in_progress"},
    })
    failed = json.dumps({
        "type": "item.completed",
        "item": {"id": "m1", "type": "mcp_tool_call", "server": "quimera", "tool": "read_file",
                 "arguments": {"path": "README.md"}, "status": "failed", "error": {"message": "sem acesso"}},
    })

    [start] = _format_codex_spy_event(started)
    [end] = _format_codex_spy_event(failed)

    assert start.text == build_tool_line("read_file", {"path": "README.md"}).to_text()
    assert start.data["tool_call_id"] == end.data["tool_call_id"] == "m1"
    assert start.data["operation"] == "start"
    assert end.data["operation"] == "end"
    assert end.data["status"] == "error"
    assert end.text.endswith("· sem acesso")


def test_codex_diff_preview_keeps_tool_call_id():
    events = _codex_command(
        "completed", "git diff", id="d1", exit_code=0, aggregated_output="diff --git a b\n+x"
    )

    diffs = [event for event in events if event.kind == "diff"]
    assert diffs and all(event.data["tool_call_id"] == "d1" for event in diffs)


# --- opencode ---------------------------------------------------------------

def test_opencode_call_id_becomes_tool_call_id_and_tail():
    started = json.dumps({"type": "tool_call", "part": {
        "type": "tool", "tool": "grep", "callID": "call_1", "input": {"pattern": "x"}}})
    finished = json.dumps({"type": "tool_result", "part": {
        "type": "tool", "tool": "grep", "callID": "call_1", "input": {"pattern": "x"},
        "status": "completed", "state": {"output": "a\nb\n"}}})

    [start] = _format_opencode_spy_event(started)
    [end] = _format_opencode_spy_event(finished)

    assert start.data["tool_call_id"] == end.data["tool_call_id"] == "call_1"
    assert start.data["line"]["tool_call_id"] == "call_1"
    assert end.data["operation"] == "end"
    assert end.data["line"]["tail"] == "b"


# --- presenter --------------------------------------------------------------

def _start(command, tool_id):
    line = build_tool_line("exec_command", {"cmd": command}, tool_call_id=tool_id)
    return SpyEvent(kind="tool", text=line.to_text(), transient=True, data={
        "tool": "exec_command", "operation": "start", "status": "running",
        "input": {"cmd": command}, "tool_call_id": tool_id, "line": line.to_payload()})


def test_presenter_sends_content_tool_line_and_id_to_feed():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", _start("ls", "t1"))

    message = renderer.show_tool_preview.call_args.args[0]
    assert set(message) == {"content", "tool_line", "tool_call_id"}
    assert message["content"] == "⌕ ls"
    assert message["tool_line"]["text"] == "⌕ ls"
    assert message["tool_call_id"] == "t1"
    assert renderer.show_tool_preview.call_args.kwargs == {"agent": "codex"}
    renderer.show_feed.assert_not_called()
    assert presenter.current_status_label == "⌕ ls"


def test_presenter_omits_tool_call_id_when_unknown():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="", transient=True, data={
        "tool": "exec_command", "operation": "start", "status": "running", "input": {"cmd": "ls"}}))

    assert set(renderer.show_tool_preview.call_args.args[0]) == {"content", "tool_line"}


def test_presenter_does_not_dedupe_equal_commands_with_distinct_ids():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", _start("ls", "t1"))
    presenter.emit("codex", _start("ls", "t2"))
    presenter.emit("codex", _start("ls", "t2"))

    ids = [call.args[0]["tool_call_id"] for call in renderer.show_tool_preview.call_args_list]
    assert ids == ["t1", "t2"]


def test_presenter_diff_preview_targets_tool_call_id():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)
    presenter.emit("codex", _start("git diff", "t1"))

    presenter.emit("codex", SpyEvent(kind="diff", text="+nova", final=True, data={
        "tool": "exec_command", "tool_call_id": "t1", "operation": "preview", "line_index": 0}))

    assert renderer.show_tool_preview.call_args.args[0] == {"content": "+nova", "preview_for": "t1"}


def test_presenter_diff_without_id_uses_text_path():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="diff", text="+nova", final=True))

    renderer.show_feed.assert_called_once_with("+nova", agent="codex", muted=True)
    renderer.show_tool_preview.assert_not_called()


def test_presenter_plain_renderer_keeps_text_path():
    renderer = MagicMock()
    renderer.supports_agent_feed = False
    presenter = SpyOutputPresenter(renderer, Visibility.QUIET)

    presenter.emit("codex", _start("ls", "t1"))

    renderer.show_plain.assert_called_once_with("⌕ ls", agent="codex", muted=True)
    renderer.show_tool_preview.assert_not_called()


def test_presenter_telemetry_requires_data():
    presenter = SpyOutputPresenter(_feed_renderer(), Visibility.SUMMARY)

    assert presenter.tool_activity(SpyEvent(kind="tool", text="$ ls")) is None
    assert presenter.tool_activity(_start("ls", "t1")) == {
        "text": "⌕ ls", "tool": "exec_command", "operation": "start",
        "status": "running", "tool_call_id": "t1",
    }


def test_presenter_api_driver_result_inherits_start_line():
    renderer = _feed_renderer()
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit_tool_call("agent", "read_file", {"path": "README.md"}, tool_call_id="call_1")
    presenter.emit_tool_result(
        "agent",
        SimpleNamespace(ok=False, tool_name="read_file", error="sem acesso", data={"tool_call_id": "call_1"}),
    )

    start, end = (call.args[0] for call in renderer.show_tool_preview.call_args_list)
    assert start["tool_line"]["status"] == "running"
    assert end["tool_line"]["status"] == "error"
    assert end["tool_line"]["subject"] == "README.md"
    assert end["tool_line"]["tail"] == "sem acesso"
    assert end["tool_call_id"] == "call_1"


# --- client (executor MCP) --------------------------------------------------

def test_client_executor_callback_builds_tool_line_for_internal_mcp():
    renderer = _feed_renderer()
    client = AgentClient(renderer)
    executor = SimpleNamespace(set_tool_preview_callback=MagicMock())
    client.bind_tool_preview_callback(executor, agent="codex")
    callback = executor.set_tool_preview_callback.call_args.args[0]

    callback("read_file", {"path": "README.md"}, {"tool_call_id": "call_5"})

    message = renderer.show_tool_preview.call_args.args[0]
    assert message["content"] == "⌕ ler README.md"
    assert message["tool_line"]["tool_call_id"] == "call_5"
    assert message["tool_line"]["transport"] == ""
    assert message["tool_call_id"] == "call_5"
    assert "run_id" not in message


def test_client_executor_callback_preserves_http_context():
    renderer = _feed_renderer()
    client = AgentClient(renderer)
    executor = SimpleNamespace(set_tool_preview_callback=MagicMock())
    client.bind_tool_preview_callback(executor)
    callback = executor.set_tool_preview_callback.call_args.args[0]

    callback(
        "read_file",
        {"path": "README.md"},
        {
            "tool_call_id": "call_6",
            "trusted_context": SimpleNamespace(
                agent_name=None, transport="http_mcp", server_origin="mcp_http", run_id="http:run-1"
            ),
        },
    )

    message = renderer.show_tool_preview.call_args.args[0]
    assert message["tool_line"]["transport"] == "mcp_http"
    assert message["content"] == "◇ ⌕ ler README.md"
    assert message["run_id"] == "http:run-1"
    assert message["transport"] == "mcp_http"
    assert message["tool_call_id"] == "call_6"
    assert renderer.show_tool_preview.call_args.kwargs["agent"] == "mcp-http"
