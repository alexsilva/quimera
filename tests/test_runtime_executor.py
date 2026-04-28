from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.task_executor import TaskExecutor


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


@pytest.fixture
def approval_handler():
    return MagicMock()


def test_executor_denied(config, approval_handler):
    # Line 58-59 coverage
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="write_file", arguments={"path": "test.py", "content": "print(1)"})
    approval_handler.approve.return_value = False
    result = executor.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_apply_patch_requires_approval(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="apply_patch", arguments={"patch": "*** Begin Patch\n*** End Patch"})
    approval_handler.approve.return_value = False
    result = executor.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_unexpected_exception(config, approval_handler):
    # Line 64-65 coverage
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="list_files", arguments={"path": "/tmp"})
    with patch.object(executor.registry, "get") as mock_get:
        mock_handler = MagicMock(side_effect=Exception("Boom"))
        mock_get.return_value = mock_handler
        result = executor.execute(call)
        assert result.ok is False
        assert "Falha inesperada: Boom" in result.error


def test_maybe_execute_from_response_parse_error(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    response = '<tool function="read_file" arguments="{invalid}" />'
    text, result = executor.maybe_execute_from_response(response)
    assert result.ok is False
    assert result.tool_name == "parse"


def test_maybe_execute_from_response_none(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    text, result = executor.maybe_execute_from_response("no tool")
    assert result is None


def test_executor_registers_interactive_command_tools(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    names = executor.registry.names()
    assert "run_shell_command" in names
    assert "exec_command" in names
    assert "write_stdin" in names
    assert "close_command_session" in names


def test_executor_normalizes_run_alias_with_commands_list(tmp_path):
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.approval_handler.approve.return_value = True
    result = executor.execute(ToolCall(name="run", arguments={"commands": ["echo hello"]}))
    assert result.ok is True
    assert "hello" in result.content


def test_executor_normalizes_execute_command_alias(tmp_path):
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.approval_handler.approve.return_value = True
    result = executor.execute(ToolCall(name="execute_command", arguments={"command": "echo hello"}))
    assert result.ok is True
    assert result.data["status"] == "completed"


def test_task_executor_skips_review_claim_when_agent_is_not_operational(tmp_path):
    executor = TaskExecutor("gemini", db_path=tmp_path / "tasks.db", poll_interval=0)
    executor.set_review_handler(lambda _task: True)
    executor.set_review_eligibility(lambda: False)
    executor._running = True

    def stop_loop(*_args, **_kwargs):
        executor._running = False
        return True

    with patch("quimera.runtime.task_executor.claim_task", return_value=None), patch(
            "quimera.runtime.task_executor.claim_review_task"
    ) as claim_review_task, patch.object(executor, "_wait_or_stop", side_effect=stop_loop):
        executor._poll_loop()

    claim_review_task.assert_not_called()


# ── remove_file executor ─────────────────────────────────────

def test_executor_remove_file_is_registered(config, approval_handler):
    """remove_file está registrado no executor."""
    executor = ToolExecutor(config, approval_handler)
    assert "remove_file" in executor.registry.names()


def test_executor_remove_file_denied_by_approval(tmp_path):
    """remove_file com aprovação negada retorna erro."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=tmp_path),
        MagicMock(),
    )
    executor.approval_handler.approve.return_value = False

    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    result = executor.execute(call)

    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_remove_file_allowed_and_executes(tmp_path):
    """remove_file com aprovação concedida executa e remove o arquivo."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=tmp_path),
        MagicMock(),
    )
    executor.approval_handler.approve.return_value = True

    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    result = executor.execute(call)

    assert result.ok is True
    assert "removido" in result.content.lower()
    assert not (tmp_path / "x.txt").exists()


def test_executor_remove_file_policy_blocks_missing_dry_run(tmp_path):
    """Política bloqueia remove_file sem dry_run=False explícito."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=tmp_path),
        MagicMock(),
    )
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt"})
    result = executor.execute(call)

    assert result.ok is False
    assert "dry_run=False" in result.error
