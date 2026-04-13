import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.config import ToolRuntimeConfig
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

    with patch("quimera.runtime.task_executor.claim_task", return_value=None), patch(
        "quimera.runtime.task_executor.claim_review_task"
    ) as claim_review_task, patch("quimera.runtime.task_executor.time.sleep", side_effect=stop_loop):
        executor._poll_loop()

    claim_review_task.assert_not_called()
