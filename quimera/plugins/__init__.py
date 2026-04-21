"""Componentes de `quimera.plugins.__init__`."""
from quimera.plugins.base import AgentPlugin, _registry, all_names, all_plugins, get, register
from . import chatgpt as _chatgpt  # noqa: F401
from . import claude as _claude  # noqa: F401
from . import codex as _codex  # noqa: F401
from . import gemini as _gemini  # noqa: F401
from . import ollama as _ollama  # noqa: F401
from . import opencode as _opencode  # noqa: F401
from .base import apply_connection_overrides  # noqa: F401

apply_connection_overrides()

__all__ = ["AgentPlugin", "_registry", "register", "get", "all_names", "all_plugins"]
