from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="opencode",
    prefix="/opencode",
    cmd=["opencode", "run"],
    style=("blue", "OpenCode"),
)
register(plugin)
