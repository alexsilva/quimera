from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="qwen",
    prefix="/qwen",
    cmd=["ollama", "run", "qwen3-coder:30b"],
    style=("green", "Qwen"),
    capabilities=["code_review"],
    preferred_task_types=["code_review"],
    avoid_task_types=[],
    supports_tools=False,
    supports_code_editing=False,
    supports_long_context=False,
    supports_task_execution=False,
    base_tier=1,
)
register(plugin)
