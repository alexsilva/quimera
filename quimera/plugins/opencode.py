from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="opencode",
    prefix="/opencode",
    cmd=["opencode", "--model=opencode/big-pickle", "run"],
    style=("blue", "OpenCode"),
)
register(plugin)
