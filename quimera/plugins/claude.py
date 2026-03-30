from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="claude",
    prefix="/claude",
    cmd=["claude", "-p"],
    style=("blue", "Claude"),
)
register(plugin)
