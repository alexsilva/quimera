from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools import shell as shell_module
from quimera.runtime.tools.shell import CommandSession, ShellTool


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


def test_shell_tool_run_basic(config):
    tool = ShellTool(config)
    call = ToolCall(name="run_shell", arguments={"command": "echo hello"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="hello\n", stderr="", returncode=0)
        result = tool.run_shell(call)
        assert result.ok is True
        assert result.content == "stdout:\nhello\n"
        assert result.data["command"] == "echo hello"
        assert result.data["stdout"] == "hello\n"


def test_shell_tool_with_staging_warning(config):
    # Line 21 coverage
    tool = ShellTool(config)
    call = ToolCall(name="run_shell", arguments={"command": "ls"})
    with patch("quimera.runtime.tools.files.get_staging_root") as mock_staging:
        mock_staging.return_value = Path("/tmp/staging")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            with pytest.warns(UserWarning, match="Shell writes bypass staging isolation"):
                tool.run_shell(call)


def test_exec_command_completes_and_returns_payload(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    call = ToolCall(
        name="exec_command",
        arguments={"cmd": 'python -u -c "print(\'hello\')"', "yield_time_ms": 200},
    )
    result = tool.exec_command(call)
    assert result.ok is True
    assert result.data["status"] == "completed"
    assert "hello" in result.data["stdout"]
    assert result.data["diff"] == [{"op": "replace", "text": "hello\n"}]
    assert result.exit_code == 0
    assert "status: completed" in result.content
    assert "stdout:\nhello\n" in result.content


def test_exec_command_supports_polling_running_process(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": 'python -u -c "import time; print(\'start\'); time.sleep(0.2); print(\'done\')"',
                "yield_time_ms": 10,
            },
        )
    )
    assert started.ok is True
    assert started.data["status"] == "running"
    assert started.data["diff"]
    session_id = started.data["session_id"]
    assert f"session_id: {session_id}" in started.content
    assert "status: running" in started.content

    finished = tool.write_stdin(
        ToolCall(
            name="write_stdin",
            arguments={"session_id": session_id, "chars": "", "yield_time_ms": 400},
        )
    )
    assert finished.ok is True
    assert finished.data["status"] == "completed"
    assert "start" in finished.data["stdout"]
    assert "done" in finished.data["stdout"]
    assert finished.data["diff"] == [{"op": "replace", "text": "start\ndone\n"}]
    assert f"session_id: {session_id}" in finished.content
    assert "status: completed" in finished.content


def test_exec_command_supports_stdin_roundtrip(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": 'python -u -c "import sys; print(sys.stdin.readline().strip())"',
                "yield_time_ms": 10,
            },
        )
    )
    assert started.ok is True
    assert started.data["status"] == "running"
    session_id = started.data["session_id"]

    finished = tool.write_stdin(
        ToolCall(
            name="write_stdin",
            arguments={
                "session_id": session_id,
                "chars": "hello from stdin\n",
                "close_stdin": True,
                "yield_time_ms": 300,
            },
        )
    )
    assert finished.ok is True
    assert finished.data["status"] == "completed"
    assert "hello from stdin" in finished.data["stdout"]


def test_close_command_session_terminates_running_process(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": 'python -u -c "import time; print(\'start\'); time.sleep(5)"',
                "yield_time_ms": 10,
            },
        )
    )
    assert started.data["status"] == "running"
    session_id = started.data["session_id"]

    closed = tool.close_command_session(
        ToolCall(name="close_command_session", arguments={"session_id": session_id})
    )
    assert closed.ok is True
    assert closed.data["status"] == "closed"
    assert f"session_id: {session_id}" in closed.content
    assert "status: closed" in closed.content
    assert session_id not in tool._sessions


def test_exec_command_supports_tty_mode(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    result = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": 'python -u -c "print(\'tty-ok\')"',
                "yield_time_ms": 200,
                "tty": True,
            },
        )
    )
    assert result.ok is True
    assert result.data["status"] == "completed"
    assert "tty-ok" in result.data["stdout"]


def test_truncate_consumed_chunks_releases_stdout_without_stderr(config):
    tool = ShellTool(config)
    session = CommandSession(
        session_id=1,
        process=MagicMock(),
        command="echo hello",
        cwd=Path("/tmp"),
        started_at=0.0,
        stdout_buffer="hello\n",
        stderr_buffer="",
        stdout_history="hello\n",
        stderr_history="",
        stdout_offset=6,
        stderr_offset=0,
        _stdout_total=6,
        _stderr_total=0,
    )

    tool._truncate_consumed_chunks(session)

    assert session.stdout_buffer == ""
    assert session.stderr_buffer == ""
    assert session.stdout_offset == 0
    assert session.stderr_offset == 0
    assert session.stdout_history == "hello\n"
    assert session.stderr_history == ""
    assert session._stdout_total == 6
    assert session._stderr_total == 0


def test_drain_session_output_returns_only_new_suffix(config):
    tool = ShellTool(config)
    session = CommandSession(
        session_id=1,
        process=MagicMock(),
        command="echo hello",
        cwd=Path("/tmp"),
        started_at=0.0,
        stdout_buffer="start\n",
        stderr_buffer="",
        stdout_history="start\n",
        stderr_history="",
        stdout_offset=6,
        stderr_offset=0,
        _stdout_total=6,
        _stderr_total=0,
    )
    session.stdout_buffer += "done\n"
    session.stdout_history += "done\n"
    session._stdout_total = len(session.stdout_buffer)

    stdout, stderr = tool._drain_session_output(session)

    assert stdout == "done\n"
    assert stderr == ""
    assert session.stdout_buffer == "done\n"
    assert session.stdout_offset == len("done\n")


def test_exec_command_enforces_session_limit_on_session_creation(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    fake_process = MagicMock()
    fake_process.poll.return_value = None

    with patch.object(tool, "_spawn_process", return_value=(fake_process, None)), patch.object(
        tool, "_start_reader_threads"
    ), patch.object(tool, "_collect_session_result") as mock_collect, patch.object(
        tool, "_enforce_session_limit", wraps=tool._enforce_session_limit
    ) as mock_enforce:
        mock_collect.return_value = MagicMock(ok=True, data={"status": "running"})
        tool.exec_command(ToolCall(name="exec_command", arguments={"cmd": "sleep 1"}))

    mock_enforce.assert_called_once()


def test_create_session_evicts_oldest_without_holding_sessions_lock(tmp_path):
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    first_process = MagicMock()
    first_process.poll.return_value = None
    original_cleanup_resources = tool._cleanup_session_resources

    def checking_cleanup(session: CommandSession, *, terminate: bool = False) -> None:
        assert not tool._sessions_lock.locked()
        original_cleanup_resources(session, terminate=terminate)

    with patch.object(shell_module, "_MAX_SESSIONS", 1):
        with patch.object(tool, "_cleanup_session_resources", side_effect=checking_cleanup) as mock_cleanup:
            first = tool._create_session(
                first_process,
                command="first",
                cwd=tmp_path,
                tty=False,
                tty_master_fd=None,
            )
            second = tool._create_session(
                MagicMock(),
                command="second",
                cwd=tmp_path,
                tty=False,
                tty_master_fd=None,
            )

    assert mock_cleanup.call_count == 1
    cleanup_session = mock_cleanup.call_args.args[0]
    assert cleanup_session.session_id == first.session_id
    assert mock_cleanup.call_args.kwargs == {"terminate": True}
    assert first.session_id not in tool._sessions
    assert second.session_id in tool._sessions
    first_process.terminate.assert_called_once()
