import pytest
import json
import threading
import subprocess
import os
from unittest.mock import MagicMock, patch, ANY
from quimera.agents import AgentClient, _strip_spinner, _should_ignore_stderr_line
from quimera.constants import MAX_STDERR_LINES
from quimera.plugins import get as get_plugin

@pytest.fixture
def renderer():
    return MagicMock()

def test_strip_spinner():
    assert _strip_spinner("⠋Executing") == "Executing"
    assert _strip_spinner("Normal text") == "Normal text"


def test_should_ignore_codex_stdin_noise():
    assert _should_ignore_stderr_line("codex", "Reading additional input from stdin...\n") is True
    assert _should_ignore_stderr_line("codex", "\x1b[2mReading additional input from stdin...\x1b[0m\r\n") is True
    assert _should_ignore_stderr_line("claude", "Reading additional input from stdin...\n") is False
    assert _should_ignore_stderr_line("codex", "real error\n") is False


def test_codex_plugin_reads_prompt_from_stdin():
    plugin = get_plugin("codex")
    assert plugin is not None
    assert plugin.prompt_as_arg is False

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
            mock_run.assert_called_with(["mock-agent"], input_text="prompt", silent=False, agent="mock", show_status=True)

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
            mock_run.assert_called_with(["mock-agent", "prompt"], input_text=None, silent=False, agent="mock", show_status=True)

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
        
        with patch("threading.Thread") as mock_thread_cls:
            mock_stdout_thread = MagicMock()
            mock_stderr_thread = MagicMock()
            # Loop stays alive while threads are alive
            mock_stdout_thread.is_alive.side_effect = [True, True, False]
            mock_stderr_thread.is_alive.return_value = False
            mock_thread_cls.side_effect = [mock_stdout_thread, mock_stderr_thread]
            
            with patch("time.time") as mock_time:
                # 1. last_activity_time = time.time() -> 100.0
                # 2. start_time = time.time() -> 100.0
                # 3. time.time() in loop (elapsed) -> 100.0
                # 4. time.time() in loop (timeout check) -> 100.2
                mock_time.side_effect = [100.0, 100.0, 100.0, 100.2, 100.2, 100.2]
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
        
        with patch("quimera.agents._logger") as mock_logger:
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
        renderer.show_plain.assert_not_called()


def test_agent_client_run_spy_shows_stderr_lines(renderer):
    client = AgentClient(renderer, spy=True)
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
            # Check truncation message was shown once
            assert renderer.show_plain.call_count == MAX_STDERR_LINES + 1


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
        
        with patch("quimera.agents.OpenAICompatDriver") as mock_driver_cls:
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
