from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="qwen",
    prefix="/qwen",
    cmd=["ollama", "run", "qwen3-coder:30b"],
    style=("green", "Qwen"),
    capabilities=["code_editing", "bug_investigation", "general_coding"],
    preferred_task_types=["code_edit", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=False,
    supports_code_editing=True,
    supports_long_context=False, base_tier=1,
)
register(plugin)
