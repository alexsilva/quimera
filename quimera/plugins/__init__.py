"""Componentes de `quimera.plugins.__init__`."""
from quimera.plugins.base import AgentPlugin, all_names, all_plugins, get, register, remove_connection

TEST_PLUGIN_NAMES = ("fake-cli", "fake-cli-handoff", "fake-openai", "fake-openai-mcp-cli")


def enable_test_plugins() -> tuple[str, ...]:
    """Registra plugins fake apenas quando o modo de teste é solicitado."""
    from .fake import register_fake_plugins

    register_fake_plugins()
    return TEST_PLUGIN_NAMES


from . import claude as _claude  # noqa: F401
from . import codex as _codex  # noqa: F401
from . import gemini as _gemini  # noqa: F401
from . import ollama as _ollama  # noqa: F401
from . import opencode as _opencode  # noqa: F401
from .base import apply_connection_overrides  # noqa: F401

apply_connection_overrides(exclude_names=set(TEST_PLUGIN_NAMES))

__all__ = ["AgentPlugin", "register", "get", "all_names", "all_plugins", "enable_test_plugins", "TEST_PLUGIN_NAMES"]
