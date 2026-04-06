import pytest
from quimera.plugins import get
import quimera.plugins.mock # Ensure registration

def test_mock_plugin_registered():
    plugin = get("mock")
    assert plugin is not None
    assert plugin.name == "mock"
    assert plugin.prefix == "/mock"
    assert "echo" in plugin.cmd
    assert plugin.prompt_as_arg is True
