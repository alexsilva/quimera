from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="qwen",
    prefix="/qwen",
    cmd=["ollama", "run", "qwen2.5-coder:14b"],
    style=("green", "Qwen"),
)
register(plugin)
