from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="codex",
    prefix="/codex",
    cmd=["codex", "--ask-for-approval=never", "exec", "--skip-git-repo-check"],
    style=("green", "Codex"),
    capabilities=["code_editing", "test_execution", "bug_investigation", "tool_use"],
    preferred_task_types=["code_edit", "test_execution", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=True,
)
register(plugin)
