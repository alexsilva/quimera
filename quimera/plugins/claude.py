from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="claude",
    prefix="/claude",
    cmd=["claude", "--permission-mode=dontAsk", "-p"],
    style=("blue", "Claude"),
)
register(plugin)
