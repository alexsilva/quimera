"""Componentes de `quimera.profiles.__init__`."""
# ruff: noqa: E402

from quimera.profiles.base import (
    ExecutionProfile,
    all_names,
    all_profiles,
    get,
    register,
    remove_connection,
)

TEST_PROFILE_NAMES = ("fake-cli", "fake-cli-delegate", "fake-openai", "fake-openai-mcp-cli")


def enable_test_profiles() -> tuple[str, ...]:
    """Registra profiles fake apenas quando o modo de teste é solicitado."""
    from .fake import register_fake_profiles

    register_fake_profiles()
    return TEST_PROFILE_NAMES


from . import antigravity as _antigravity  # noqa: F401
from . import claude as _claude  # noqa: F401
from . import codex as _codex  # noqa: F401
from . import opencode as _opencode  # noqa: F401
from .base import apply_connections  # noqa: F401

apply_connections(exclude_names=set(TEST_PROFILE_NAMES))

__all__ = [
    "ExecutionProfile",
    "TEST_PROFILE_NAMES",
    "all_names",
    "all_profiles",
    "enable_test_profiles",
    "get",
    "register",
    "remove_connection",
]
