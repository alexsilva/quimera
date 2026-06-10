import pytest

import quimera.plugins.mock  # noqa: F401
from quimera.plugins import get


def test_mock_plugin_registered():
    """Verifica que o plugin mock é registrado com os atributos esperados."""
    plugin = get("mock")
    assert plugin is not None
    assert plugin.name == "mock"
    assert plugin.prefix == "/mock"
    assert "echo" in plugin.cmd
    assert plugin.prompt_as_arg is True
