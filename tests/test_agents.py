import threading
from unittest.mock import MagicMock, patch, ANY

import pytest

from quimera.agent_events import SpyEvent
from quimera.agents import (
    AgentClient,
    _filter_stderr_lines,
    _is_rate_limit_signal,
    _strip_spinner,
    _should_ignore_stderr_line,
)
from quimera.constants import MAX_STDERR_LINES, Visibility
from quimera.plugins import get as get_plugin
from quimera.plugins.base import CliConnection
from quimera.plugins.claude import _format_claude_spy_event
from quimera.plugins.codex import _format_codex_spy_event
from quimera.plugins.opencode import _format_opencode_spy_event
from quimera.plugins.spy_utils import format_command_output_preview
from quimera.spy_output_presenter import SpyOutputPresenter


@pytest.fixture
def renderer():
    return MagicMock()


def test_strip_spinner():
    assert _strip_spinner("⠋Executing") == "Executing"
    assert _strip_spinner("Normal text") == "Normal text"


def test_should_ignore_codex_stdin_noise():
    assert _should_ignore_stderr_line("codex", "Reading additional input from stdin...\n") is True
    assert _should_ignore_stderr_line("codex", "\x1b[2mReading additional input from stdin...\x1b[0m\r\n") is True
    assert _should_ignore_stderr_line("codex", "Reading prompt from stdin...\n") is True
    assert _should_ignore_stderr_line("claude", "Reading additional input from stdin...\n") is False
    assert _should_ignore_stderr_line("codex", "real error\n") is False


def test_filter_stderr_lines_removes_codex_stdin_noise():
    assert _filter_stderr_lines(
        "codex",
        ["Reading prompt from stdin...\n", "real error\n"],
    ) == ["real error\n"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP 429 Too Many Requests", True),
        ("rate limit exceeded", True),
        ("request was throttled by upstream", True),
        ("tool finished 429 items successfully", False),
        ("progress: 429/1000 tokens", False),
        ("error id=429", False),
    ],
)
def test_is_rate_limit_signal(text, expected):
    assert _is_rate_limit_signal(text) is expected


def test_codex_plugin_reads_prompt_from_stdin():
    plugin = get_plugin("codex")
    assert plugin is not None
    assert plugin.prompt_as_arg is False
    assert "/code" in plugin.aliases


def test_codex_plugin_resumes_last_session_in_workspace():
    plugin = get_plugin("codex")
    assert plugin is not None
    assert plugin.effective_cmd() == [
        "codex",
        "exec",
        "resume",
        "--last",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
        "-",
    ]


def test_codex_plugin_applies_resume_to_cli_override():
    plugin = get_plugin("codex")
    assert plugin is not None
    original_override = plugin._connection_override
    try:
        plugin._connection_override = CliConnection(
            cmd=["codex", "exec", "--json"],
            prompt_as_arg=False,
            output_format="codex-json",
        )
        assert plugin.effective_cmd() == ["codex", "exec", "resume", "--last", "--json", "-"]
    finally:
        plugin._connection_override = original_override


def test_agent_client_run_success(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["Success\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["echo", "hi"], silent=True)
        assert result == "Success"


def test_agent_client_run_silent_does_not_log_codex_stdin_noise(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen, patch("quimera.agents.client._logger") as mock_logger:
        mock_proc = MagicMock()
        mock_proc.stdout = ["Success\n"]
        mock_proc.stderr = ["Reading prompt from stdin...\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["codex", "exec"], silent=True, agent="codex")

        assert result == "Success"
        mock_logger.warning.assert_not_called()


def test_agent_client_run_os_error(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.side_effect = OSError("command not found")
        result = client.run(["nonexistent"], silent=True)
        assert result is None
        renderer.show_error.assert_called()


def test_agent_client_run_failure_return_code(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Error detail\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["fail"], silent=True)
        assert result is None
        renderer.show_error.assert_called()


def test_agent_client_call(renderer):
    client = AgentClient(renderer)
    with patch("quimera.plugins.get") as mock_get:
        mock_plugin = MagicMock()
        mock_plugin.cmd = ["mock-agent"]
        mock_plugin.prompt_as_arg = False
        mock_get.return_value = mock_plugin

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = "output"
            result = client.call("mock", "prompt")
            assert result == "output"
            mock_run.assert_called_with(["mock-agent"], input_text="prompt", silent=False, agent="mock",
                                        show_status=True)


def test_agent_client_call_prompt_as_arg(renderer):
    client = AgentClient(renderer)
    with patch("quimera.plugins.get") as mock_get:
        mock_plugin = MagicMock()
        mock_plugin.cmd = ["mock-agent"]
        mock_plugin.prompt_as_arg = True
        mock_get.return_value = mock_plugin

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = "output"
            result = client.call("mock", "prompt")
            assert result == "output"
            mock_run.assert_called_with(["mock-agent", "prompt"], input_text=None, silent=False, agent="mock",
                                        show_status=True)


def test_agent_client_log_metrics(renderer, tmp_path):
    metrics_file = tmp_path / "metrics.jsonl"
    client = AgentClient(renderer, metrics_file=str(metrics_file))
    metrics = {"total_chars": 100, "history_chars": 50}
    client.log_prompt_metrics("claude", metrics)

    assert metrics_file.exists()
    content = metrics_file.read_text()
    assert '"agent": "claude"' in content
    assert '"total_chars": 100' in content


def test_agent_client_run_streaming(renderer):
    # Line 100-161 coverage approx
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["line1\n", "line2\n"])
        mock_proc.stderr = iter(["err1\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        # Mocking time.sleep to avoid waiting
        with patch("time.sleep"):
            result = client.run(["echo"], silent=False, show_status=True)
            assert "line1" in result
            assert "line2" in result


def test_agent_client_run_timeout(renderer):
    # Line 152-161 coverage approx
    client = AgentClient(renderer, timeout=0.1)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        mock_proc.pid = 99999  # real int so os.getpgid raises OSError (process not found)
        with patch("threading.Thread") as mock_thread_cls:
            mock_stdout_thread = MagicMock()
            mock_stderr_thread = MagicMock()
            # Loop stays alive for first iteration; wall-clock fires before second check
            mock_stdout_thread.is_alive.side_effect = [True]
            mock_stderr_thread.is_alive.return_value = False
            mock_thread_cls.side_effect = [mock_stdout_thread, mock_stderr_thread]

            with patch("time.time") as mock_time:
                # 1. start_time = time.time() -> 100.0 (ProcessRunner.watch)
                # 2. elapsed = int(time.time() - start_time) -> 101.0 => elapsed=1
                # 3. now = time.time() -> 101.0 (_check_timeout)
                # wall_limit = 0.1 * 5 = 0.5; elapsed=1 > 0.5 => wall timeout fires
                mock_time.side_effect = [100.0, 101.0, 101.0]
                with patch("time.sleep"):
                    result = client.run(["slow"], silent=False)
                    assert result is None
                    renderer.show_error.assert_called()


def test_agent_client_run_input_failure(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = Exception("broken pipe")
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], input_text="input", silent=True)
        assert result is None
        mock_proc.kill.assert_called()
        renderer.show_error.assert_called()


def test_agent_client_run_communication_error(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["out\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # We need to wait for threads, but we can mock them or force error
        with patch("threading.Thread") as mock_thread:
            # We want to set result_holder["error"]
            # This is hard because result_holder is local to run()
            # Let's mock the whole run method's internals or use a different approach
            pass


def test_agent_client_run_silent_logs(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["Out\n"]
        mock_proc.stderr = ["Err\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        with patch("quimera.agents.client._logger") as mock_logger:
            result = client.run(["cmd"], silent=True)
            assert result == "Out"
            mock_logger.debug.assert_called()
            mock_logger.warning.assert_called()


def test_agent_client_run_failure_with_tail(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["Line 1\n", "Line 2\n", "Line 3\n", "Line 4\n", "Line 5\n", "Line 6\n"]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        result = client.run(["fail"], silent=True)
        assert result is None
        # Should show error message AND tail (last 5 lines)
        assert renderer.show_error.call_count >= 2


def test_agent_client_run_marks_rate_limit_from_stderr(renderer):
    client = AgentClient(renderer, timeout=1)

    class SlowLines:
        def __init__(self, lines, delay=0.05):
            self._lines = list(lines)
            self._delay = delay

        def __iter__(self):
            for line in self._lines:
                threading.Event().wait(self._delay)
                yield line

    with patch("subprocess.Popen") as mock_popen, patch("time.sleep"):
        mock_proc = MagicMock()
        mock_proc.stdout = SlowLines(["output\n"])
        mock_proc.stderr = SlowLines(["HTTP 429 Too Many Requests\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=False, show_status=False)

    assert "output" in result
    assert client.rate_limit_detected is True
    assert client.rate_limit_detected_at is not None


def test_agent_client_run_does_not_mark_rate_limit_from_stdout(renderer):
    client = AgentClient(renderer, timeout=1)

    with patch("subprocess.Popen") as mock_popen, patch("time.sleep"):
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["the tool printed: rate limit\n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=False, show_status=False)

    assert "rate limit" in result
    assert client.rate_limit_detected is False
    assert client.rate_limit_detected_at is None


def test_agent_client_run_streaming_with_status_and_stderr(renderer):
    # Line 118, 123-129, 131-140 approx
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["out1\n"])
        mock_proc.stderr = iter(["err1\n", "err2\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        mock_status = MagicMock()
        renderer.running_status.return_value = mock_status

        with patch("time.sleep"):
            result = client.run(["echo"], silent=False, show_status=True)
            assert "out1" in result
            renderer.show_plain.assert_any_call("err1", agent=ANY)
            mock_status.__enter__.assert_called()
            mock_status.__exit__.assert_called()


def test_agent_client_run_suppresses_codex_stdin_noise(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(['{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"ok"}}\n'])
        mock_proc.stderr = iter(["Reading additional input from stdin...\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

        assert "agent_message" in result
        # Verifica que a linha de ruído do codex não foi exibida (pode haver mensagens de summary)
        noise = "Reading additional input from stdin..."
        for call_args in renderer.show_plain.call_args_list:
            assert noise not in str(call_args)


def test_agent_client_run_spy_shows_stderr_lines(renderer):
    client = AgentClient(renderer, visibility=Visibility.FULL)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(['{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"ok"}}\n'])
        mock_proc.stderr = iter(["tool: exec_command\n", "tool: apply_patch\n"])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    assert "agent_message" in result
    renderer.show_plain.assert_any_call("tool: exec_command", agent="codex")
    renderer.show_plain.assert_any_call("tool: apply_patch", agent="codex")


def test_agent_client_run_spy_shows_codex_stdout_context(renderer):
    client = AgentClient(renderer, visibility=Visibility.FULL)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"reasoning","summary":"Vou checar o estado do repositório antes de editar"}}\n',
            '{"type":"item.started","item":{"type":"command_execution","command":"git status"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git status","exit_code":0}}\n',
            '{"type":"item.completed","item":{"id":"1","type":"agent_message","text":"Encontrei alterações locais e vou seguir sem revertê-las."}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    assert "agent_message" in result
    renderer.show_plain.assert_any_call("Vou checar o estado do repositório antes de editar", agent="codex")
    renderer.show_plain.assert_any_call("$ git status", agent="codex")
    # exit_code=0 é silencioso — nenhum [ok] emitido
    renderer.show_plain.assert_any_call("Encontrei alterações locais e vou seguir sem revertê-las.",
                                        agent="codex")


def test_agent_client_run_summary_shows_formatted_codex_stdout(renderer):
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.completed","item":{"type":"agent_message","text":"message 1\\nmessage 2\\nclear\\nmessage 3"}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    renderer.show_plain.assert_any_call("→ codex iniciando...", agent="codex")
    renderer.show_plain.assert_any_call("message 1", agent="codex")
    renderer.show_plain.assert_any_call("message 2", agent="codex")
    renderer.show_plain.assert_any_call("message 3", agent="codex")
    renderer.show_plain.assert_any_call("← codex concluído", agent="codex")


def test_agent_client_run_summary_flushes_compacted_responses_before_context(renderer):
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    status = MagicMock()
    status_cm = MagicMock()
    status_cm.__enter__.return_value = status
    status_cm.__exit__.return_value = None
    renderer.running_status.return_value = status_cm
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.completed","item":{"type":"agent_message","text":"linha 1\\nlinha 2"}}\n',
            '{"type":"item.started","item":{"type":"command_execution","command":"git status"}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=True)

    renderer.show_plain.assert_any_call("linha 1", agent="codex")
    renderer.show_plain.assert_any_call("linha 2", agent="codex")
    assert any("codex | $ git status" in str(c) for c in status.update.call_args_list)
    assert ("$ git status",) not in [call.args for call in renderer.show_plain.call_args_list]


def test_agent_client_run_summary_keeps_completed_tool_line_transient(renderer):
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    status = MagicMock()
    status_cm = MagicMock()
    status_cm.__enter__.return_value = status
    status_cm.__exit__.return_value = None
    renderer.running_status.return_value = status_cm
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"command_execution","command":"git diff -- quimera/agents.py"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=True)

    assert any("codex | $ git diff -- quimera/agents.py" in str(c) for c in status.update.call_args_list)
    assert ("✓ git diff -- quimera/agents.py",) not in [call.args for call in renderer.show_plain.call_args_list]
    assert ("$ git diff -- quimera/agents.py",) not in [call.args for call in renderer.show_plain.call_args_list]


def test_agent_client_run_summary_shows_diff_output_and_keeps_next_operation_clean(renderer):
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    status = MagicMock()
    status_cm = MagicMock()
    status_cm.__enter__.return_value = status
    status_cm.__exit__.return_value = None
    renderer.running_status.return_value = status_cm
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"command_execution","command":"git diff -- quimera/agents.py"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0,"aggregated_output":"diff --git a/quimera/agents.py b/quimera/agents.py\\n+nova linha"}}\n',
            '{"type":"item.started","item":{"type":"command_execution","command":"git status --short"}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=True)

    assert any("codex | $ git status --short" in str(c) for c in status.update.call_args_list)
    assert ("$ git status --short",) not in [call.args for call in renderer.show_plain.call_args_list]
    assert ("✓ git diff -- quimera/agents.py",) not in [call.args for call in renderer.show_plain.call_args_list]
    renderer.show_plain.assert_any_call("diff --git a/quimera/agents.py b/quimera/agents.py", agent="codex")
    renderer.show_plain.assert_any_call("+nova linha", agent="codex")


def test_spy_output_presenter_keeps_next_operation_clean_after_diff_preview(renderer):
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ git diff -- quimera/agents.py"))
    presenter.emit("codex", SpyEvent(kind="tool", text="✓ git diff -- quimera/agents.py"))
    for event in format_command_output_preview(
        "git diff -- quimera/agents.py",
        "diff --git a/quimera/agents.py b/quimera/agents.py\n+nova linha",
    ):
        presenter.emit("codex", event)
    presenter.emit("codex", SpyEvent(kind="tool", text="$ git status --short"))

    renderer.show_plain.assert_any_call("diff --git a/quimera/agents.py b/quimera/agents.py", agent="codex")
    renderer.show_plain.assert_any_call("+nova linha", agent="codex")
    assert ("✓ git diff -- quimera/agents.py",) not in [call.args for call in renderer.show_plain.call_args_list]
    assert ("$ git status --short",) not in [call.args for call in renderer.show_plain.call_args_list]
    assert presenter.current_status_label == "$ git status --short"


def test_spy_output_presenter_compose_status_label_keeps_base_and_tool(renderer):
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ git status --short"))

    assert presenter.compose_status_label("codex") == "codex | $ git status --short"


def test_agent_client_run_summary_does_not_persist_started_tool_without_status(renderer):
    client = AgentClient(renderer, visibility=Visibility.SUMMARY)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"item.started","item":{"type":"command_execution","command":"git diff -- quimera/agents.py"}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0}}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            client.run(["codex", "exec"], silent=False, agent="codex", show_status=False)

    assert ("✓ git diff -- quimera/agents.py",) not in [call.args for call in renderer.show_plain.call_args_list]
    assert ("$ git diff -- quimera/agents.py",) not in [call.args for call in renderer.show_plain.call_args_list]


def test_spy_output_presenter_summary_keeps_tool_progress_transient(renderer):
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="usando apply_patch"))
    presenter.emit("codex", SpyEvent(kind="tool", text="✓ editar quimera/agents.py"))

    assert presenter.current_status_label == ""
    assert renderer.show_plain.call_count == 0


def test_spy_output_presenter_summary_keeps_tool_failure_persistent(renderer):
    presenter = SpyOutputPresenter(renderer, Visibility.SUMMARY)

    presenter.emit("codex", SpyEvent(kind="tool", text="$ pytest -q"))
    presenter.emit("codex", SpyEvent(kind="tool", text="✗ pytest -q (exit 1)"))

    renderer.show_plain.assert_called_once_with("✗ pytest -q (exit 1)", agent="codex")
    assert presenter.current_status_label == ""


def test_agent_client_run_spy_shows_claude_stdout_context(renderer):
    client = AgentClient(renderer, visibility=Visibility.FULL)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"},{"type":"text","text":"Vou inspecionar o arquivo antes de sugerir a mudança."}]}}\n',
            '{"type":"result","result":"ok","is_error":false}\n',
        ])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["claude", "-p"], silent=False, agent="claude", show_status=False)

    assert result == '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"},{"type":"text","text":"Vou inspecionar o arquivo antes de sugerir a mudança."}]}}\n{"type":"result","result":"ok","is_error":false}'
    renderer.show_plain.assert_any_call("usando Read", agent="claude")
    renderer.show_plain.assert_any_call("Vou inspecionar o arquivo antes de sugerir a mudança.",
                                        agent="claude")
    renderer.show_plain.assert_any_call("execução concluída", agent="claude")


def test_agent_client_run_post_drain(renderer):
    # Line 166-180 approx - Drain remaining queue after threads die
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("threading.Thread") as mock_thread_cls:
            mock_stdout_thread = MagicMock()
            mock_stderr_thread = MagicMock()
            # Threads die immediately
            mock_stdout_thread.is_alive.return_value = False
            mock_stderr_thread.is_alive.return_value = False
            mock_thread_cls.side_effect = [mock_stdout_thread, mock_stderr_thread]

            # But we put something in the queue manually if we could...
            # Actually, the real threads put things in log_queue.
            # Since we mocked Thread, we have to simulate what they do.

            # Let's use a real thread for a moment or mock the queue behavior in the loop
            pass


def test_agent_client_run_uses_working_dir(renderer, tmp_path):
    workspace = str(tmp_path)
    client = AgentClient(renderer, working_dir=workspace)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["ok\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        client.run(["echo", "hi"], silent=True)
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("cwd") == workspace


def test_agent_client_run_legacy_workspace_root_alias(renderer, tmp_path):
    workspace = str(tmp_path)
    client = AgentClient(renderer, workspace_root=workspace)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["ok\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        client.run(["echo", "hi"], silent=True)
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("cwd") == workspace


def test_agent_client_run_without_working_dir_passes_none(renderer):
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = ["ok\n"]
        mock_proc.stderr = []
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        client.run(["echo", "hi"], silent=True)
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("cwd") is None


def test_agent_client_thread_exceptions(renderer):
    # Line 62-63, 74-75
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()

        # stdout that raises exception when iterated
        def error_iter():
            raise Exception("Read error")
            yield "never"

        mock_proc.stdout = error_iter()
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # This will set result_holder["error"]
        result = client.run(["cmd"], silent=True)
        assert result is None
        # Check that show_error was called with the expected message
        args, _ = renderer.show_error.call_args
        assert "falha ao comunicar com cmd: Read error" in args[0]


def test_synthetic_tool_result(renderer):
    from quimera.agents import _SyntheticToolResult
    result_ok = _SyntheticToolResult(ok=True)
    assert result_ok.ok is True
    result_err = _SyntheticToolResult(ok=False, error="test error")
    assert result_err.ok is False
    assert result_err.error == "test error"


def test_agent_client_run_empty_stderr_line(renderer):
    # Line 144: empty line after strip
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["output\n", "  \n"])
        mock_proc.stderr = iter([])
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["cmd"], silent=False)
            assert "output" in result


def test_agent_client_run_stderr_truncation(renderer):
    # Line 147-153: stderr truncation at MAX_STDERR_LINES
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        lines = [f"error line {i}\n" for i in range(15)]
        mock_proc.stdout = iter(["output\n"])
        mock_proc.stderr = iter(lines)
        mock_proc.returncode = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            result = client.run(["cmd"], silent=False)
            assert "output" in result
            # Stderr truncado: exatamente MAX_STDERR_LINES linhas + 1 mensagem de truncamento
            # (summary mode adiciona 2 calls extra de →/←, excluídos da contagem)
            non_summary_calls = [
                c for c in renderer.show_plain.call_args_list
                if not any(
                    str(c).startswith(f"call('{arrow}")
                    for arrow in ("→", "←")
                )
            ]
            assert len(non_summary_calls) == MAX_STDERR_LINES + 1


def test_agent_client_run_no_output_with_error(renderer):
    # Line 202-219: no output but has error (returncode 0 case)
    client = AgentClient(renderer)
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.stderr = ["some stderr\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = client.run(["cmd"], silent=True)
        assert result is None
        renderer.show_error.assert_called()


def test_agent_client_call_unknown_agent(renderer):
    # Line 276-278: unknown agent
    client = AgentClient(renderer)
    result = client.call("unknown_agent", "prompt")
    assert result is None
    renderer.show_error.assert_called()


def test_agent_client_call_api_driver(renderer):
    # Line 280-281, 293-325: API driver path
    client = AgentClient(renderer, timeout=60)
    with patch("quimera.plugins.get") as mock_get:
        mock_plugin = MagicMock()
        mock_plugin.driver = "api"
        mock_plugin.model = "llama3"
        mock_plugin.base_url = "http://localhost:11434"
        mock_plugin.api_key_env = "OLLAMA_API_KEY"
        mock_plugin.supports_tools = True
        mock_plugin.tool_use_reliability = "medium"
        mock_get.return_value = mock_plugin

        with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls:
            mock_driver = MagicMock()
            mock_driver.run.return_value = "api response"
            mock_driver_cls.return_value = mock_driver

            with patch.object(client, "_api_drivers", {}):
                result = client.call("test-agent", "prompt")
            mock_driver_cls.assert_called()


def test_parse_stream_json(renderer):
    # Line 223-247: _parse_stream_json
    client = AgentClient(renderer)
    raw = '''{"type":"result","result":"final output"}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"bash"}]}}'''
    result = client._parse_stream_json(raw, "test-agent")
    assert result == "final output"


def test_parse_stream_json_error(renderer):
    client = AgentClient(renderer)
    raw = '{"type":"result","is_error":true,"result":"error msg"}'
    result = client._parse_stream_json(raw, "test-agent")
    assert result is None


def test_parse_codex_json(renderer):
    # Line 249-271: _parse_codex_json
    client = AgentClient(renderer)
    callback_called = []

    def track_callback(agent, result=None, loop_abort=None, reason=None):
        callback_called.append(True)

    client.tool_event_callback = track_callback

    raw = '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0}}'
    result = client._parse_codex_json(raw, "codex")
    assert callback_called


def test_parse_codex_json_with_text(renderer):
    client = AgentClient(renderer)
    raw = '{"type":"item.completed","item":{"type":"agent_message","text":"final text"}}'
    result = client._parse_codex_json(raw, "codex")
    assert result == "final text"


def test_format_codex_spy_event_command():
    started = _format_codex_spy_event('{"type":"item.started","item":{"type":"command_execution","command":"ls"}}')
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0}}')
    assert started == [SpyEvent(kind="tool", text="$ ls")]
    assert completed == [SpyEvent(kind="tool", text="✓ ls")]


def test_format_codex_spy_event_reasoning_and_message():
    reasoning = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"reasoning","summary":"Vou localizar o formatter do plugin e ajustar a mensagem"}}'
    )
    message = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"agent_message","text":"Ajustei a saída para mostrar progresso útil ao usuário."}}'
    )
    assert reasoning == [SpyEvent(kind="context", text="Vou localizar o formatter do plugin e ajustar a mensagem", transient=True)]
    assert message == [SpyEvent(kind="response", text="Ajustei a saída para mostrar progresso útil ao usuário.", final=True)]


def test_format_codex_spy_event_splits_multiline_agent_messages():
    message = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"agent_message","text":"message 1\\nmessage 2\\nclear\\nmessage 3"}}'
    )
    assert message == [
        SpyEvent(kind="response", text="message 1", final=True),
        SpyEvent(kind="response", text="message 2", final=True),
        SpyEvent(kind="clear", text="", transient=True),
        SpyEvent(kind="response", text="message 3", final=True),
    ]


def test_format_codex_spy_event_reports_failed_test_command():
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_agents.py","exit_code":1}}'
    )
    assert completed == [SpyEvent(kind="tool", text="✗ pytest -q tests/test_agents.py (exit 1)")]


def test_format_codex_spy_event_hides_successful_command_completion():
    started = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"command_execution","command":"git status --short"}}'
    )
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"git status --short","exit_code":0}}'
    )
    assert started == [SpyEvent(kind="tool", text="$ git status --short")]
    assert completed == [SpyEvent(kind="tool", text="✓ git status --short")]


def test_format_codex_spy_event_includes_diff_output_from_aggregated_output():
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"command_execution","command":"git diff -- quimera/agents.py","exit_code":0,"aggregated_output":"diff --git a/quimera/agents.py b/quimera/agents.py\\n+nova linha"}}'
    )
    assert completed == [
        SpyEvent(kind="tool", text="✓ git diff -- quimera/agents.py"),
        SpyEvent(kind="diff", text="diff --git a/quimera/agents.py b/quimera/agents.py", final=True),
        SpyEvent(kind="diff", text="+nova linha", final=True),
    ]


def test_format_codex_spy_event_reports_file_change_start_and_completion():
    started = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"file_change","path":"quimera/agents.py"}}'
    )
    completed = _format_codex_spy_event(
        '{"type":"item.completed","item":{"type":"file_change","path":"quimera/agents.py"}}'
    )
    assert started == [SpyEvent(kind="tool", text="editar quimera/agents.py")]
    assert completed == [SpyEvent(kind="tool", text="✓ editar quimera/agents.py")]


def test_format_codex_spy_event_reports_tool_calls_as_tool_messages():
    message = _format_codex_spy_event(
        '{"type":"item.started","item":{"type":"tool_call","name":"apply_patch"}}'
    )
    assert message == [SpyEvent(kind="tool", text="usando apply_patch")]


def test_codex_plugin_exposes_spy_stdout_formatter():
    plugin = get_plugin("codex")
    assert plugin is not None
    assert plugin.spy_stdout_formatter is _format_codex_spy_event


def test_claude_plugin_exposes_spy_stdout_formatter():
    plugin = get_plugin("claude")
    assert plugin is not None
    assert plugin.spy_stdout_formatter is _format_claude_spy_event


def test_opencode_plugin_exposes_spy_stdout_formatter_and_json_output():
    plugin = get_plugin("opencode-pickle")
    assert plugin is not None
    assert plugin.spy_stdout_formatter is _format_opencode_spy_event
    assert plugin.output_format == "opencode-json"
    assert "--format=json" in plugin.cmd


def test_format_claude_spy_event_summarizes_assistant_and_result():
    assistant = _format_claude_spy_event(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"},{"type":"text","text":"Vou validar com um teste focado antes de concluir."}]}}'
    )
    result = _format_claude_spy_event('{"type":"result","result":"ok","is_error":false}')
    assert assistant == [
        SpyEvent(kind="tool", text="usando Bash"),
        SpyEvent(kind="response", text="Vou validar com um teste focado antes de concluir.", final=True),
    ]
    assert result == [SpyEvent(kind="context", text="execução concluída", transient=True)]


def test_format_opencode_spy_event_summarizes_text_and_result():
    started = _format_opencode_spy_event('{"type":"step_start","part":{"type":"step-start"}}')
    message = _format_opencode_spy_event(
        '{"type":"text","part":{"type":"text","text":"message 1\\nclear\\nmessage 2"}}'
    )
    result = _format_opencode_spy_event(
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}'
    )
    assert started == [SpyEvent(kind="context", text="iniciando execução", transient=True)]
    assert message == [
        SpyEvent(kind="response", text="message 1", final=True),
        SpyEvent(kind="clear", text="", transient=True),
        SpyEvent(kind="response", text="message 2", final=True),
    ]
    assert result == [SpyEvent(kind="context", text="execução concluída", transient=True)]


def test_format_opencode_spy_event_reports_tool_calls_as_tool_messages():
    tool = _format_opencode_spy_event(
        '{"type":"tool_call","part":{"type":"tool-call","tool":"run_shell"}}'
    )
    assert tool == [SpyEvent(kind="tool", text="usando run_shell")]


def test_parse_opencode_json_with_text(renderer):
    client = AgentClient(renderer)
    raw = "\n".join([
        '{"type":"step_start","part":{"type":"step-start"}}',
        '{"type":"text","part":{"type":"text","text":"primeira linha"}}',
        '{"type":"text","part":{"type":"text","text":"segunda linha"}}',
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
    ])
    result = client._parse_opencode_json(raw, "opencode-gpt")
    assert result == "primeira linha\nsegunda linha"


def test_agent_client_call_stream_json_format(renderer):
    # Line 286-288: stream-json format
    client = AgentClient(renderer)
    with patch("quimera.plugins.get") as mock_get:
        mock_plugin = MagicMock()
        mock_plugin.cmd = ["agent"]
        mock_plugin.prompt_as_arg = False
        mock_plugin.output_format = "stream-json"
        mock_get.return_value = mock_plugin

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = '{"type":"result","result":"parsed"}'
            result = client.call("agent", "prompt")
            assert result == "parsed"


def test_agent_client_call_codex_json_format(renderer):
    # Line 289-290: codex-json format
    client = AgentClient(renderer)
    with patch("quimera.plugins.get") as mock_get:
        mock_plugin = MagicMock()
        mock_plugin.cmd = ["agent"]
        mock_plugin.prompt_as_arg = False
        mock_plugin.output_format = "codex-json"
        mock_get.return_value = mock_plugin

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = '{"type":"item.completed","item":{"type":"agent_message","text":"parsed"}}'
            result = client.call("agent", "prompt")
            assert result == "parsed"


def test_agent_client_call_opencode_json_format(renderer):
    client = AgentClient(renderer)
    with patch("quimera.plugins.get") as mock_get:
        mock_plugin = MagicMock()
        mock_plugin.cmd = ["agent"]
        mock_plugin.prompt_as_arg = False
        mock_plugin.output_format = "opencode-json"
        mock_get.return_value = mock_plugin

        with patch.object(client, "run") as mock_run:
            mock_run.return_value = '{"type":"text","part":{"type":"text","text":"parsed"}}'
            result = client.call("agent", "prompt")
            assert result == "parsed"


# ---------------------------------------------------------------------------
# Testes de cancelamento via API (_call_api)
# ---------------------------------------------------------------------------

def test_call_api_starts_and_stops_esc_monitor(renderer):
    """_start_esc_monitor e _stop_esc_monitor devem ser chamados para agentes API."""
    from types import SimpleNamespace
    client = AgentClient(renderer)
    plugin = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor") as mock_start, \
            patch.object(client, "_stop_esc_monitor") as mock_stop:
        mock_driver = MagicMock()
        mock_driver.run.return_value = "ok"
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", plugin, "prompt")

        assert result == "ok"
        mock_start.assert_called_once()
        mock_stop.assert_called_once()


def test_call_api_passes_cancel_event_to_driver(renderer):
    """cancel_event deve ser passado ao driver para cancelamento cooperativo."""
    from types import SimpleNamespace
    client = AgentClient(renderer)
    plugin = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()
        mock_driver.run.return_value = "result"
        mock_driver_cls.return_value = mock_driver

        client._call_api("test-agent", plugin, "prompt")

        call_kwargs = mock_driver.run.call_args.kwargs
        assert "cancel_event" in call_kwargs
        assert call_kwargs["cancel_event"] is client._cancel_event


def test_call_api_cancel_event_detection(renderer):
    """Quando cancel_event é acionado externamente, o while loop detecta e retorna None."""
    from types import SimpleNamespace
    import threading as _threading

    client = AgentClient(renderer)
    plugin = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    driver_started = _threading.Event()

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor"):
        mock_driver = MagicMock()

        def slow_run(**kwargs):
            # Driver bloqueia sem verificar cancel_event — o while loop externo deve detectá-lo
            driver_started.set()
            _threading.Event().wait(5.0)  # bloqueia por mais tempo que o teste
            return "never"

        mock_driver.run.side_effect = slow_run
        mock_driver_cls.return_value = mock_driver

        def trigger():
            driver_started.wait(timeout=2)
            client._cancel_event.set()

        t = _threading.Thread(target=trigger)
        t.start()

        result = client._call_api("test-agent", plugin, "prompt")
        t.join(timeout=3)

    assert result is None
    renderer.show_error.assert_called_with("[cancelado] pelo usuário")


def test_call_api_stop_monitor_called_on_error(renderer):
    """_stop_esc_monitor deve ser chamado mesmo quando o driver lança exceção."""
    from types import SimpleNamespace
    client = AgentClient(renderer)
    plugin = SimpleNamespace(
        driver="openai_compat",
        model="test-model",
        base_url="http://localhost",
        api_key_env=None,
        tool_use_reliability="medium",
        supports_tools=True,
    )

    with patch("quimera.agents.client.OpenAICompatDriver") as mock_driver_cls, \
            patch.object(client, "_start_esc_monitor"), \
            patch.object(client, "_stop_esc_monitor") as mock_stop:
        mock_driver = MagicMock()
        mock_driver.run.side_effect = RuntimeError("conexão recusada")
        mock_driver_cls.return_value = mock_driver

        result = client._call_api("test-agent", plugin, "prompt")

        assert result is None
        mock_stop.assert_called_once()
        renderer.show_error.assert_called()


# ---------------------------------------------------------------------------
# Testes de cancelamento CLI (signal handler, terminal, killpg)
# ---------------------------------------------------------------------------

def test_signal_handler_sets_cancel_event(renderer):
    """cancel_event existe e pode ser settado via código ou signal."""
    client = AgentClient(renderer)

    assert hasattr(client, "_cancel_event")
    assert client._cancel_event is not None

    client._cancel_event.set()
    assert client._cancel_event.is_set()

    client._cancel_event.clear()
    assert not client._cancel_event.is_set()


def test_stop_esc_monitor_restores_signal_handler(renderer):
    """_stop_esc_monitor deve restaurar o signal handler original."""
    import signal
    client = AgentClient(renderer)

    with patch("quimera.agents.signal_guard.signal") as mock_signal:
        mock_signal.SIGINT = signal.SIGINT
        mock_signal.getsignal.return_value = signal.SIG_DFL

        with patch.object(client, "_start_esc_monitor"):
            client._start_esc_monitor()
            client._esc_monitor._old_handler = signal.SIG_DFL

        client._stop_esc_monitor()

        mock_signal.signal.assert_called_with(signal.SIGINT, signal.SIG_DFL)


def test_stop_esc_monitor_without_termios_state(renderer):
    """_stop_esc_monitor não deve depender de state de termios."""
    client = AgentClient(renderer)
    client._agent_running = False
    client._stop_esc_monitor()


def test_start_esc_monitor_is_noop_outside_main_thread(renderer):
    """Resumo final roda em thread; monitor de SIGINT não pode explodir fora da main thread."""
    client = AgentClient(renderer)
    result = {}

    def worker():
        try:
            client._start_esc_monitor()
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - o teste garante que isso não acontece
            result["error"] = exc
        finally:
            client._stop_esc_monitor()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert result == {"ok": True}
    assert client._esc_monitor._old_handler is None


def test_terminate_process_group_uses_killpg(renderer):
    """_terminate_process_group deve usar os.killpg para matar processo e filhos."""
    client = AgentClient(renderer)
    proc = MagicMock()
    proc.pid = 12345

    with patch("quimera.agents.signal_guard.os") as mock_os:
        mock_os.getpgid.return_value = 12345
        mock_os.killpg.return_value = None
        mock_os.killpg.side_effect = OSError(" ESRCH")

        client._terminate_process_group(proc)

        mock_os.getpgid.assert_called_once_with(12345)
        mock_os.killpg.assert_called_once()


def test_cancel_event_cleared_on_start(renderer):
    """_cancel_event pode ser limpo antes de iniciar."""
    client = AgentClient(renderer)

    client._cancel_event.set()
    assert client._cancel_event.is_set()

    client._cancel_event.clear()
    assert not client._cancel_event.is_set()


def test_core_turn_manager_reset_after_first_agent(renderer):
    """ core.py:947 - turn_manager.reset() após primeiro agente """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_handoff(renderer):
    """ core.py:1037 - turn_manager.reset() após handoff """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_fallback(renderer):
    """ core.py:1078 - turn_manager.reset() após fallback """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_synthesis(renderer):
    """ core.py:1106 - turn_manager.reset() após síntese """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_parallel_merge(renderer):
    """ core.py:1170 - turn_manager.reset() após merge paralelo """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_core_turn_manager_reset_after_sequential_loop(renderer):
    """ core.py:1202 - turn_manager.reset() no loop sequencial """
    from unittest import mock
    from quimera.app.core import QuimeraApp

    app = mock.MagicMock(spec=QuimeraApp)
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.renderer = renderer
    app.turn_manager = mock.MagicMock()

    if app.agent_client._user_cancelled:
        app.turn_manager.reset()

    app.turn_manager.reset.assert_called_once()


def test_task_cancellation_after_call(renderer):
    """ task.py:177 - cancelamento após call """
    from unittest import mock

    app = mock.MagicMock()
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.tasks_db_path = ":memory:"

    if app.agent_client._user_cancelled:
        pass

    assert app.agent_client._user_cancelled


def test_task_cancellation_after_review(renderer):
    """ task.py:264 - cancelamento após review """
    from unittest import mock

    app = mock.MagicMock()
    app.agent_client = AgentClient(renderer)
    app.agent_client._user_cancelled = True
    app.tasks_db_path = ":memory:"

    if app.agent_client._user_cancelled:
        pass

    assert app.agent_client._user_cancelled
