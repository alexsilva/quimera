"""Componentes de `quimera.plugins.antigravity`."""
from quimera.plugins.base import AgentPlugin, register


class AntigravityPlugin(AgentPlugin):
    """Plugin do Antigravity CLI (agy)."""


plugin = AntigravityPlugin(
    name="antigravity",
    prefix="/antigravity",
    icon="🪐",
    cmd=["agy", "--dangerously-skip-permissions", "-p"],
    style=("cyan", "Antigravity"),
    prompt_as_arg=True,
    output_format=None,
    capabilities=["code_review", "documentation", "general_reasoning", "code_editing", "complex_refactoring"],
    preferred_task_types=["code_review", "documentation", "general", "code_edit", "architecture", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    base_tier=3,
)
register(plugin)
