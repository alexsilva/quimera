import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from quimera.config import ConfigManager
from quimera.runtime.config import ToolRuntimeConfig
from quimera.sandbox.bwrap import SandboxError
from quimera.workspace import Workspace
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicyError
from quimera.runtime.tools import shell as shell_module
from quimera.runtime.tools.shell import CommandSession, ShellTool, ShellToolValidator


def test_shell_tool_run_basic(config):
    """Verifica que Test shell tool run basic."""
    tool = ShellTool(config)
    call = ToolCall(name="run_shell", arguments={"command": "echo hello"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="hello\n", stderr="", returncode=0)
        result = tool.run_shell(call)
        assert result.ok is True
        assert "exit_code: 0" in result.content
        assert "stdout:\nhello\n" in result.content
        assert result.exit_code == 0
        assert result.data["exit_code"] == 0
        assert result.data["timed_out"] is False
        assert "duration_ms" in result.data
        assert result.data["command"] == "echo hello"
        assert result.data["stdout"] == "hello\n"


def test_run_shell_nonzero_exit_preserves_process_diagnostics(config):
    """Exit code não-zero mantém stderr e metadados no conteúdo da tool."""
    tool = ShellTool(config)
    call = ToolCall(name="run_shell", arguments={"command": "probe"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="falha real\n", returncode=7)

        result = tool.run_shell(call)

    assert result.ok is False
    assert result.error is None
    assert result.exit_code == 7
    assert "exit_code: 7" in result.content
    assert "stderr:\nfalha real\n" in result.content


def test_run_shell_uses_default_timeout_when_omitted(tmp_path):
    """Sem timeout explícito, run_shell usa o default configurado."""
    config = ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        command_timeout_seconds=20,
        command_max_timeout_seconds=300,
    )
    tool = ShellTool(config)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        tool.run_shell(ToolCall(name="run_shell", arguments={"command": "echo ok"}))

    assert mock_run.call_args.kwargs["timeout"] == 20


def test_run_shell_fails_closed_when_workspace_sandbox_is_unavailable(tmp_path):
    workspace = Workspace(tmp_path)
    ConfigManager(workspace.workspace_config_file).set_sandbox_enabled(True)
    tool = ShellTool(ToolRuntimeConfig(workspace=workspace))

    with patch("quimera.sandbox.bwrap._find_bwrap_executable", return_value=None), patch(
        "subprocess.run"
    ) as run:
        result = tool.run_shell(
            ToolCall(name="run_shell", arguments={"command": "echo blocked"})
        )

    assert result.ok is False
    assert "sandbox do workspace está ativo" in result.error
    run.assert_not_called()


def test_run_shell_allows_timeout_above_default_up_to_configured_max(tmp_path):
    """Timeout explícito pode exceder o default sem exceder o teto da runtime."""
    config = ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        command_timeout_seconds=20,
        command_max_timeout_seconds=300,
    )
    tool = ShellTool(config)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        tool.run_shell(
            ToolCall(name="run_shell", arguments={"command": "echo ok", "timeout": 60})
        )

    assert mock_run.call_args.kwargs["timeout"] == 60


def test_run_shell_caps_requested_timeout_at_configured_max(tmp_path):
    """Timeout solicitado acima do teto é limitado pelo máximo da runtime."""
    config = ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        command_timeout_seconds=20,
        command_max_timeout_seconds=120,
    )
    tool = ShellTool(config)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        tool.run_shell(
            ToolCall(name="run_shell", arguments={"command": "echo ok", "timeout": 300})
        )

    assert mock_run.call_args.kwargs["timeout"] == 120


def test_run_shell_timeout_preserves_partial_bytes_and_nullable_exit_code(config):
    tool = ShellTool(config)
    timeout = shell_module.subprocess.TimeoutExpired(
        cmd="slow",
        timeout=0.1,
        output=b"partial stdout\n",
        stderr=b"partial stderr\n",
    )
    with patch("subprocess.run", side_effect=timeout):
        result = tool.run_shell(
            ToolCall(name="run_shell", arguments={"command": "slow", "timeout": 0.1})
        )

    assert result.ok is False
    assert result.exit_code is None
    assert result.truncated is False
    assert result.data["stdout"] == "partial stdout\n"
    assert result.data["stderr"] == "partial stderr\n"
    assert result.data["timed_out"] is True
    assert "partial stdout" in result.content


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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    command = tool._rewrite_command_for_local_venv("pytest tests/test_x.py -q", tmp_path)

    assert command == f"{pytest_bin} tests/test_x.py -q"


def test_rewrite_python3_falls_back_to_virtualenv_python(tmp_path):
    """Usa `.venv/bin/python` quando o comando é `python3` e só `python` existe."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.write_text("#!/bin/sh\n")
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    command = tool._rewrite_command_for_local_venv("python3 -m pytest -q", tmp_path)

    assert command == f"{python_bin} -m pytest -q"


def test_rewrite_virtualenv_preserves_shell_chaining(tmp_path):
    """A reescrita do executável não deve transformar operadores em argumentos."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.write_text("#!/bin/sh\n")
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    command = tool._rewrite_command_for_local_venv("python script.py && echo ok", tmp_path)

    assert command == f"{python_bin} script.py && echo ok"


def test_workspace_environment_uses_project_virtualenv_for_indirect_python(tmp_path, monkeypatch):
    """O ambiente da tool deve isolar subprocessos do virtualenv do Quimera."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    venv_bin = workspace / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.symlink_to(sys.executable)

    quimera_venv = tmp_path / "quimera" / ".venv"
    quimera_bin = quimera_venv / "bin"
    quimera_bin.mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(quimera_venv))
    monkeypatch.setenv("PATH", f"{quimera_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(workspace)))
    result = _poll_until_completed(
        tool,
        tool.exec_command(
            ToolCall(
                name="exec_command",
                arguments={
                    "cmd": (
                        "python -c 'import os, subprocess, sys; "
                        "print(os.environ.get(\"VIRTUAL_ENV\")); "
                        "print(subprocess.check_output([\"python\", \"-c\", "
                        "\"import sys; print(sys.executable)\"], text=True).strip())'"
                    ),
                    "login": False,
                    "yield_time_ms": 500,
                },
            )
        ),
    )

    assert result.ok is True
    lines = result.data["stdout"].splitlines()
    assert lines[0] == str(workspace / ".venv")
    assert lines[1] == str(python_bin)


def test_workspace_environment_removes_quimera_virtualenv_without_project_venv(tmp_path, monkeypatch):
    """Workspace sem `.venv` não deve herdar o virtualenv interno do Quimera."""
    quimera_venv = tmp_path / "quimera" / ".venv"
    quimera_bin = quimera_venv / "bin"
    quimera_bin.mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(quimera_venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(quimera_bin), "/usr/bin", "/bin"]))

    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    env = tool._build_workspace_environment(tmp_path)

    assert "VIRTUAL_ENV" not in env
    assert str(quimera_bin) not in env["PATH"].split(os.pathsep)


def test_workspace_environment_finds_root_virtualenv_from_nested_workdir(tmp_path, monkeypatch):
    """Workdir interno continua usando o `.venv` da raiz da workspace."""
    workspace = tmp_path / "workspace"
    nested = workspace / "src" / "package"
    nested.mkdir(parents=True)
    workspace_bin = workspace / ".venv" / "bin"
    workspace_bin.mkdir(parents=True)

    quimera_venv = tmp_path / "quimera" / ".venv"
    quimera_bin = quimera_venv / "bin"
    quimera_bin.mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(quimera_venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(quimera_bin), "/usr/bin", "/bin"]))

    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(workspace)))
    env = tool._build_workspace_environment(nested)

    assert env["VIRTUAL_ENV"] == str(workspace / ".venv")
    assert env["PATH"].split(os.pathsep)[0] == str(workspace_bin)


def test_run_shell_supports_workdir(tmp_path):
    subdir = tmp_path / "pkg"
    subdir.mkdir()
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    call = ToolCall(name="run_shell", arguments={"command": "pwd", "workdir": "pkg"})

    result = tool.run_shell(call)

    assert result.ok is True
    assert result.data["cwd"] == str(subdir)
    assert result.data["stdout"].strip() == str(subdir)


def test_session_output_truncation_is_reported(tmp_path):
    config = ToolRuntimeConfig(workspace=Workspace(tmp_path), max_output_chars=32)
    tool = ShellTool(config)
    call = ToolCall(
        name="exec_command",
        arguments={"cmd": f'{sys.executable} -u -c "print(\'x\' * 200)"', "yield_time_ms": 500},
    )

    result = _poll_until_completed(tool, tool.exec_command(call))

    assert result.truncated is True
    assert len(result.data["stdout"]) == 32


def test_developer_chaining_validates_every_command(tmp_path):
    from quimera.runtime.workspace_policy import WorkspacePolicy

    config = ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
        workspace_policy=WorkspacePolicy.developer(),
    )
    validator = ShellToolValidator(config)

    validator.validate(ToolCall(name="run_shell", arguments={"command": "rg foo | head"}))
    with pytest.raises(ToolPolicyError, match="fora da allowlist: nc"):
        validator.validate(ToolCall(name="run_shell", arguments={"command": "rg foo | nc host 9"}))


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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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


def test_exec_command_completed_result_includes_full_buffered_stdout(tmp_path):
    """Processo concluído não perde o tail que ainda estava nos pipes."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    expected = "".join(f"{value}\n" for value in range(1, 20001))

    result = _poll_until_completed(
        tool,
        tool.exec_command(
            ToolCall(
                name="exec_command",
                arguments={"cmd": "seq 1 20000", "yield_time_ms": 10},
            )
        ),
        yield_time_ms=500,
    )

    assert result.ok is True
    assert result.data["status"] == "completed"
    assert result.data["stdout"] == expected


def test_exec_command_supports_polling_running_process(tmp_path):
    """Verifica que Test exec command supports polling running process."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
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


def test_collect_completed_session_marks_payload_closed(config):
    tool = ShellTool(config)
    process = MagicMock()
    process.poll.return_value = 0
    session = CommandSession(
        session_id=1,
        process=process,
        command="echo done",
        cwd=Path("/tmp"),
        started_at=0.0,
        stdout_buffer="done\n",
        stdout_history="done\n",
        _stdout_total=5,
    )
    tool._sessions[1] = session

    result = tool._collect_session_result(
        session,
        yield_time_ms=1,
        tool_name="exec_command",
        include_session_id=True,
    )

    assert result.data["status"] == "completed"
    assert result.data["closed"] is True
    assert result.data["session_id"] == 1
    assert 1 not in tool._sessions


def test_close_command_session_waits_after_kill(config):
    tool = ShellTool(config)
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = [shell_module.subprocess.TimeoutExpired("cmd", 1), None]
    session = CommandSession(
        session_id=1,
        process=process,
        command="sleep",
        cwd=Path("/tmp"),
        started_at=0.0,
    )
    tool._sessions[1] = session

    result = tool.close_command_session(
        ToolCall(name="close_command_session", arguments={"session_id": 1})
    )

    assert result.ok is True
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def test_cleanup_session_resources_waits_after_kill_and_closes_streams(config):
    """Cleanup direto recolhe o processo forçado e fecha todos os pipes."""
    tool = ShellTool(config)
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = [shell_module.subprocess.TimeoutExpired("cmd", 1), None]
    session = CommandSession(
        session_id=1,
        process=process,
        command="sleep",
        cwd=Path("/tmp"),
        started_at=0.0,
    )

    tool._cleanup_session_resources(session, terminate=True)

    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()


def test_cleanup_session_resources_does_not_close_streams_with_active_reader(config):
    """Não fecha pipe por baixo de uma reader ainda bloqueada em leitura."""
    tool = ShellTool(config)
    process = MagicMock()
    process.poll.return_value = 0
    reader = MagicMock()
    reader.is_alive.return_value = True
    session = CommandSession(
        session_id=1,
        process=process,
        command="done",
        cwd=Path("/tmp"),
        started_at=0.0,
        reader_threads=[reader],
    )

    tool._cleanup_session_resources(session)

    process.stdin.close.assert_not_called()
    process.stdout.close.assert_not_called()
    process.stderr.close.assert_not_called()


def test_close_command_session_waits_reader_tail_before_draining(config):
    """Fechamento explícito espera a reader entregar o tail antes do último drain."""
    tool = ShellTool(config)
    process = MagicMock()
    process.poll.return_value = 0
    process.stdin = None
    session = CommandSession(
        session_id=1,
        process=process,
        command="seq 1 20000",
        cwd=Path("/tmp"),
        started_at=0.0,
    )

    def late_reader() -> None:
        time.sleep(0.05)
        with session.lock:
            tool._append_chunk(session, "stdout_buffer", "stdout_history", "_stdout_total", "tail\n")

    reader = threading.Thread(target=late_reader, daemon=True)
    session.reader_threads.append(reader)
    tool._sessions[1] = session
    reader.start()

    closed = tool.close_command_session(
        ToolCall(name="close_command_session", arguments={"session_id": 1})
    )

    assert closed.ok is True
    assert closed.data["stdout"] == "tail\n"


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


def test_exec_command_reserves_session_slot_before_spawning(tmp_path):
    """A capacidade é reservada antes de qualquer subprocesso ser iniciado."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    fake_process = MagicMock()
    fake_process.poll.return_value = None

    def spawn_after_reservation(*args, **kwargs):
        assert tool._session_reservations == 1
        return fake_process, None

    with patch.object(tool, "_spawn_process", side_effect=spawn_after_reservation), patch.object(
        tool, "_start_reader_threads"
    ), patch.object(tool, "_collect_session_result") as mock_collect, patch.object(
        tool, "_reserve_session_slot", wraps=tool._reserve_session_slot
    ) as mock_reserve:
        mock_collect.return_value = MagicMock(ok=True, data={"status": "running"})
        tool.exec_command(ToolCall(name="exec_command", arguments={"cmd": "sleep 1"}))

    mock_reserve.assert_called_once()
    assert tool._session_reservations == 0


def test_exec_command_releases_reserved_slot_when_spawn_is_blocked(tmp_path):
    """Falha de sandbox no spawn não pode consumir capacidade futura."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with patch.object(
        tool,
        "_spawn_process",
        side_effect=SandboxError("sandbox indisponível"),
    ):
        result = tool.exec_command(
            ToolCall(name="exec_command", arguments={"cmd": "echo blocked"})
        )

    assert result.ok is False
    assert result.error == "sandbox indisponível"
    assert tool._session_reservations == 0


def test_exec_command_releases_reserved_slot_when_spawn_raises(tmp_path):
    """Erro inesperado no spawn libera a reserva antes de propagar a exceção."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with patch.object(tool, "_spawn_process", side_effect=OSError("spawn failed")):
        with pytest.raises(OSError, match="spawn failed"):
            tool.exec_command(
                ToolCall(name="exec_command", arguments={"cmd": "echo failed"})
            )

    assert tool._session_reservations == 0


def test_tty_spawn_failure_closes_allocated_pty_fds(tmp_path):
    """Falha de Popen após openpty não pode vazar master/slave FDs."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with patch.object(tool, "_wrap_subprocess_cmd", side_effect=lambda _wd, cmd, **_kw: cmd), patch.object(
        shell_module.pty,
        "openpty",
        return_value=(100, 101),
    ), patch.object(
        shell_module.subprocess,
        "Popen",
        side_effect=OSError("popen failed"),
    ), patch.object(shell_module.os, "close") as close_fd:
        with pytest.raises(OSError, match="popen failed"):
            tool._spawn_process(
                "echo failed",
                tmp_path,
                shell="/bin/bash",
                login=False,
                tty=True,
                env={},
            )

    assert [call.args[0] for call in close_fd.call_args_list] == [100, 101]


def _register_idle_session(tool: ShellTool, *, command: str, running: bool) -> CommandSession:
    """Registra uma sessão como exec_command faria, já sem tool operando nela."""
    process = MagicMock()
    process.poll.return_value = None if running else 0
    assert tool._reserve_session_slot() is True
    session = tool._create_session(
        process,
        command=command,
        cwd=Path("/tmp"),
        tty=False,
        tty_master_fd=None,
    )
    tool._release_session_use(session)
    return session


def test_reserve_session_slot_reclaims_oldest_idle_finished_session(tmp_path):
    """Capacidade cheia libera só a sessão encerrada e ociosa mais antiga, fora do lock."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    reclaimed: list[int] = []

    def checking_cleanup(session: CommandSession) -> None:
        assert not tool._sessions_lock.locked()
        reclaimed.append(session.session_id)

    with patch.object(shell_module, "_MAX_SESSIONS", 3):
        running = _register_idle_session(tool, command="server", running=True)
        older = _register_idle_session(tool, command="old", running=False)
        newer = _register_idle_session(tool, command="new", running=False)
        running.started_at, older.started_at, newer.started_at = 0.0, 1.0, 2.0

        with patch.object(tool, "_cleanup_detached_session_resources", side_effect=checking_cleanup):
            assert tool._reserve_session_slot() is True

    assert reclaimed == [older.session_id]
    assert set(tool._sessions) == {running.session_id, newer.session_id}
    running.process.terminate.assert_not_called()
    tool._release_session_slot()


def test_reserve_session_slot_keeps_running_and_in_use_sessions(tmp_path):
    """Sessão em execução ou sendo lida por outra tool nunca é liberada."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with patch.object(shell_module, "_MAX_SESSIONS", 2):
        running = _register_idle_session(tool, command="server", running=True)
        busy = _register_idle_session(tool, command="busy", running=False)

        with tool._session_in_use(busy.session_id):
            assert tool._reserve_session_slot() is False
            assert set(tool._sessions) == {running.session_id, busy.session_id}

        assert tool._reserve_session_slot() is True

    assert set(tool._sessions) == {running.session_id}
    running.process.terminate.assert_not_called()
    tool._release_session_slot()
    assert tool._session_reservations == 0


def test_exec_command_reports_capacity_error_listing_open_sessions(tmp_path):
    """Sem sessão liberável, recusa antes do spawn, preserva a ativa e lista as abertas."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with patch.object(shell_module, "_MAX_SESSIONS", 1):
        first = _register_idle_session(tool, command="npm   run\ndev", running=True)
        with patch.object(tool, "_spawn_process") as spawn_process:
            second_result = tool.exec_command(
                ToolCall(name="exec_command", arguments={"cmd": "second"})
            )

    assert second_result.ok is False
    assert second_result.error.startswith("Limite de 1 sessões shell abertas atingido")
    assert "close_command_session" in second_result.error
    assert f"Sessões abertas: {first.session_id} (npm run dev)" in second_result.error
    assert list(tool._sessions) == [first.session_id]
    spawn_process.assert_not_called()
    first.process.terminate.assert_not_called()


def test_exec_command_reuses_capacity_of_finished_uncollected_sessions(tmp_path):
    """Sessões que terminaram sem ninguém consultá-las não travam o exec_command."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))

    with patch.object(shell_module, "_MAX_SESSIONS", 2):
        forgotten = [
            tool.exec_command(
                ToolCall(name="exec_command", arguments={"cmd": "sleep 1", "yield_time_ms": 10})
            )
            for _ in range(2)
        ]
        assert [result.data["status"] for result in forgotten] == ["running", "running"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            session.process.poll() is None for session in list(tool._sessions.values())
        ):
            time.sleep(0.02)
        assert len(tool._sessions) == 2

        result = tool.exec_command(
            ToolCall(name="exec_command", arguments={"cmd": "echo ok", "yield_time_ms": 1000})
        )

    assert result.ok is True
    assert result.data["stdout"] == "ok\n"


def test_exec_command_releases_session_use_after_collect(tmp_path):
    """A sessão nova fica protegida durante a coleta e ociosa depois dela."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    fake_process = MagicMock()
    fake_process.poll.return_value = None
    in_use_during_collect: list[int] = []

    def collect(session: CommandSession, **_kwargs):
        in_use_during_collect.append(session.in_use)
        return MagicMock(ok=True)

    with patch.object(tool, "_spawn_process", return_value=(fake_process, None)), patch.object(
        tool, "_start_reader_threads"
    ), patch.object(tool, "_collect_session_result", side_effect=collect):
        tool.exec_command(ToolCall(name="exec_command", arguments={"cmd": "sleep 1"}))

    (session,) = tool._sessions.values()
    assert in_use_during_collect == [1]
    assert session.in_use == 0


def test_exec_command_cleans_registered_session_when_reader_start_fails(tmp_path):
    """Falha após registrar a sessão não deixa processo órfão sem session_id."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0

    with patch.object(tool, "_spawn_process", return_value=(process, None)), patch.object(
        tool,
        "_start_reader_threads",
        side_effect=RuntimeError("reader start failed"),
    ):
        with pytest.raises(RuntimeError, match="reader start failed"):
            tool.exec_command(
                ToolCall(name="exec_command", arguments={"cmd": "sleep 5"})
            )

    assert tool._sessions == {}
    assert tool._session_reservations == 0
    process.terminate.assert_called_once()
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()


@pytest.mark.parametrize(
    ("tool_name", "arguments", "spied"),
    [
        ("poll_command_session", {}, "_collect_session_result"),
        ("write_stdin", {"chars": "x"}, "_collect_session_result"),
        ("close_command_session", {}, "_drain_session_output"),
    ],
)
def test_session_tools_mark_session_in_use(config, tool_name, arguments, spied):
    """Tools que operam numa sessão a protegem da liberação por capacidade."""
    tool = ShellTool(config)
    process = MagicMock()
    process.poll.return_value = 0
    session = CommandSession(
        session_id=1,
        process=process,
        command="done",
        cwd=Path("/tmp"),
        started_at=0.0,
    )
    tool._sessions[1] = session
    in_use_seen: list[int] = []

    def spy(*_args, **_kwargs):
        in_use_seen.append(session.in_use)
        return ("", "") if spied == "_drain_session_output" else MagicMock(ok=True)

    with patch.object(tool, spied, side_effect=spy):
        getattr(tool, tool_name)(
            ToolCall(name=tool_name, arguments={"session_id": 1, **arguments})
        )

    assert in_use_seen == [1]
    assert session.in_use == 0


def test_session_slot_reservation_is_atomic_under_concurrency(tmp_path):
    """Duas admissões simultâneas não podem reservar o mesmo último slot."""
    tool = ShellTool(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    barrier = threading.Barrier(3)
    results: list[bool] = []

    def reserve() -> None:
        barrier.wait()
        results.append(tool._reserve_session_slot())

    with patch.object(shell_module, "_MAX_SESSIONS", 1):
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

    assert sorted(results) == [False, True]
    assert tool._session_reservations == 1
    tool._release_session_slot()
    assert tool._session_reservations == 0
