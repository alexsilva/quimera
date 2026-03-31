from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="gemini",
    prefix="/gemini",
    cmd=["gemini", "-p"],
    style=("cyan", "Gemini"),
    prompt_as_arg=True,
)
register(plugin)
