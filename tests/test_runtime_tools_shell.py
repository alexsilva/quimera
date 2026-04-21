from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.shell import ShellTool


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
        assert "hello" in result.content


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
