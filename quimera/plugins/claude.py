"""Componentes de `quimera.plugins.claude`."""
from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="claude",
    prefix="/claude",
    cmd=["claude", "--permission-mode=dontAsk", "-p"],
    style=("blue", "Claude"),
    capabilities=["architecture", "code_review", "planning", "documentation", "code_editing"],
    preferred_task_types=["architecture", "code_review", "documentation", "code_edit", "general"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=True, base_tier=3,
)
register(plugin)
