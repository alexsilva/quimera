import pytest

import quimera.profiles.mock  # noqa: F401
from quimera.profiles import get


def test_mock_profile_registered():
    """Verifica que o profile mock é registrado com os atributos esperados."""
    profile = get("mock")
    assert profile is not None
    assert profile.name == "mock"
    assert profile.prefix == "/mock"
    assert "echo" in profile.cmd
    assert profile.prompt_as_arg is True
