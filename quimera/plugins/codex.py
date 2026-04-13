"""Componentes de `quimera.plugins.codex`."""
from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="codex",
    prefix="/codex",
    cmd=["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json"],
    output_format="codex-json",
    prompt_as_arg=True,
    style=("green", "Codex"),
    capabilities=["code_editing", "code_review","test_execution", "bug_investigation", "tool_use"],
    preferred_task_types=["code_edit", "code_review", "test_execution", "bug_investigation", "general"],
    avoid_task_types=[],
    supports_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True, base_tier=2,
)
register(plugin)
