from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="gemini",
    prefix="/gemini",
    cmd=["gemini", "--approval-mode=yolo", "-p"],
    style=("cyan", "Gemini"),
    prompt_as_arg=True,
)
register(plugin)
