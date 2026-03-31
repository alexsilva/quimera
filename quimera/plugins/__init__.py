from quimera.plugins.base import AgentPlugin, _registry, all_names, all_plugins, get, register
from . import claude as _claude  # noqa: F401
from . import codex as _codex  # noqa: F401
from . import qwen as _qwen  # noqa: F401
from . import gemini as _gemini  # noqa: F401
from . import opencode as _opencode  # noqa: F401

__all__ = ["AgentPlugin", "_registry", "register", "get", "all_names", "all_plugins"]
