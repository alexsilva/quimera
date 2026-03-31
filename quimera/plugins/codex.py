from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="codex",
    prefix="/codex",
    cmd=["codex", "exec", "--ask-for-approval=never", "--skip-git-repo-check"],
    style=("green", "Codex"),
)
register(plugin)
