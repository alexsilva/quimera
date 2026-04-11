import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall, ToolResult
from quimera.runtime.config import ToolRuntimeConfig

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
