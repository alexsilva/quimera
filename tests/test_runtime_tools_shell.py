import pytest
import warnings
from unittest.mock import patch, MagicMock
from pathlib import Path
from quimera.runtime.tools.shell import ShellTool
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall

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
