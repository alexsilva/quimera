import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicyError
from quimera.runtime.tools import shell as shell_module
from quimera.runtime.tools.shell import CommandSession, ShellTool


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


def test_shell_tool_run_basic(config):
    """Verifica que Test shell tool run basic."""
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
    """Verifica que Test shell tool with staging warning."""
    tool = ShellTool(config)
    call = ToolCall(name="run_shell", arguments={"command": "ls"})
    with patch("quimera.runtime.tools.files.get_staging_root") as mock_staging:
        mock_staging.return_value = Path("/tmp/staging")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            with pytest.warns(UserWarning, match="Shell writes bypass staging isolation"):
                tool.run_shell(call)


def test_rewrite_command_prefers_workdir_virtualenv(tmp_path):
    """Reescreve comandos Python comuns para o `.venv` do workdir alvo."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    pytest_bin = venv_bin / "pytest"
    pytest_bin.write_text("#!/bin/sh\n")
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))

    command = tool._rewrite_command_for_local_venv("pytest tests/test_x.py -q", tmp_path)

    assert command == f"{pytest_bin} tests/test_x.py -q"


def test_rewrite_python3_falls_back_to_virtualenv_python(tmp_path):
    """Usa `.venv/bin/python` quando o comando é `python3` e só `python` existe."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.write_text("#!/bin/sh\n")
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))

    command = tool._rewrite_command_for_local_venv("python3 -m pytest -q", tmp_path)

    assert command == f"{python_bin} -m pytest -q"


def _poll_until_completed(tool: ShellTool, result, *, yield_time_ms: int = 500):
    current = result
    for _ in range(5):
        if current.data.get("status") == "completed":
            return current
        current = tool.write_stdin(
            ToolCall(
                name="write_stdin",
                arguments={
                    "session_id": current.data["session_id"],
                    "chars": "",
                    "yield_time_ms": yield_time_ms,
                },
            )
        )
    return current


def test_exec_command_completes_and_returns_payload(tmp_path):
    """Verifica que Test exec command completes and returns payload."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    call = ToolCall(
        name="exec_command",
        arguments={"cmd": f'{sys.executable} -u -c "print(\'hello\')"', "yield_time_ms": 200},
    )
    result = _poll_until_completed(tool, tool.exec_command(call))
    assert result.ok is True
    assert result.data["status"] == "completed"
    assert "hello" in result.data["stdout"]
    assert result.data["diff"] == [{"op": "replace", "text": "hello\n"}]
    assert result.exit_code == 0
    assert "status: completed" in result.content
    assert "stdout:\nhello\n" in result.content


def test_exec_command_supports_polling_running_process(tmp_path):
    """Verifica que Test exec command supports polling running process."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": f'{sys.executable} -u -c "import time; print(\'start\'); time.sleep(0.2); print(\'done\')"',
                "yield_time_ms": 10,
            },
        )
    )
    assert started.ok is True
    if started.data["status"] == "running":
        session_id = started.data["session_id"]
        assert f"session_id: {session_id}" in started.content
        assert "status: running" in started.content
        finished = _poll_until_completed(
            tool,
            tool.write_stdin(
                ToolCall(
                    name="write_stdin",
                    arguments={"session_id": session_id, "chars": "", "yield_time_ms": 400},
                )
            ),
        )
    else:
        session_id = started.data["session_id"]
        finished = started
    assert finished.ok is True
    assert finished.data["status"] == "completed"
    assert "start" in finished.data["stdout"]
    assert "done" in finished.data["stdout"]
    assert finished.data["diff"] == [{"op": "replace", "text": "start\ndone\n"}]
    assert f"session_id: {session_id}" in finished.content
    assert "status: completed" in finished.content


def test_poll_command_session_reads_output_without_stdin_payload(tmp_path):
    """Consulta uma sessão em execução sem enviar chars para stdin."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": f'{sys.executable} -u -c "import time; print(\'start\'); time.sleep(0.1); print(\'done\')"',
                "yield_time_ms": 10,
            },
        )
    )
    session_id = started.data["session_id"]

    result = tool.poll_command_session(
        ToolCall(
            name="poll_command_session",
            arguments={"session_id": session_id, "yield_time_ms": 500},
        )
    )

    assert result.ok is True
    assert result.data["session_id"] == session_id
    assert result.data["status"] in {"running", "completed"}
    if result.data["status"] == "running":
        result = tool.poll_command_session(
            ToolCall(
                name="poll_command_session",
                arguments={
                    "session_id": session_id,
                    "yield_time_ms": 500,
                    "wait_for_completion": True,
                },
            )
        )
    assert result.data["status"] == "completed"
    assert "start" in result.data["stdout"]
    assert "done" in result.data["stdout"]


def test_exec_command_rejects_workdir_outside_workspace_at_runtime(tmp_path):
    """Garante que chamada direta também respeita o limite da workspace."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    call = ToolCall(
        name="exec_command",
        arguments={
            "cmd": f'{sys.executable} -u -c "print(\'hello\')"',
            "workdir": str(tmp_path.parent),
        },
    )

    with pytest.raises(ToolPolicyError, match="workdir fora da workspace"):
        tool.exec_command(call)


def test_exec_command_supports_stdin_roundtrip(tmp_path):
    """Verifica que Test exec command supports stdin roundtrip."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": f'{sys.executable} -u -c "import sys; print(sys.stdin.readline().strip())"',
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
    finished = _poll_until_completed(tool, finished, yield_time_ms=500)
    assert finished.ok is True
    assert finished.data["status"] == "completed"
    assert "hello from stdin" in finished.data["stdout"]


def test_close_command_session_terminates_running_process(tmp_path):
    """Verifica que Test close command session terminates running process."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    started = tool.exec_command(
        ToolCall(
            name="exec_command",
            arguments={
                "cmd": f'{sys.executable} -u -c "import time; print(\'start\'); time.sleep(5)"',
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
    """Verifica que Test exec command supports tty mode."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    result = _poll_until_completed(
        tool,
        tool.exec_command(
            ToolCall(
                name="exec_command",
                arguments={
                    "cmd": f'{sys.executable} -u -c "print(\'tty-ok\')"',
                    "yield_time_ms": 200,
                    "tty": True,
                },
            )
        ),
    )
    assert result.ok is True
    assert result.data["status"] == "completed"
    assert "tty-ok" in result.data["stdout"]


def test_exec_command_tty_waits_for_short_completion_after_yield(tmp_path):
    """Verifica que Test exec command tty waits for short completion after yield."""
    tool = ShellTool(ToolRuntimeConfig(workspace_root=tmp_path))
    result = _poll_until_completed(
        tool,
        tool.exec_command(
            ToolCall(
                name="exec_command",
                arguments={
                    "cmd": f'{sys.executable} -u -c "import time; time.sleep(0.25); print(\'tty-grace\')"',
                    "yield_time_ms": 100,
                    "tty": True,
                },
            )
        ),
        yield_time_ms=700,
    )
    assert result.ok is True
    assert result.data["status"] == "completed"
    assert "tty-grace" in result.data["stdout"]


def test_truncate_consumed_chunks_releases_stdout_without_stderr(config):
    """Verifica que Test truncate consumed chunks releases stdout without stderr."""
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
    """Verifica que Test drain session output returns only new suffix."""
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
    """Verifica que Test exec command enforces session limit on session creation."""
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
    """Verifica que Test create session evicts oldest without holding sessions lock."""
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
