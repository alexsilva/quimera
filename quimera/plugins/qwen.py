from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="qwen",
    prefix="/qwen",
    cmd=["ollama", "run", "qwen2.5-coder:14b"],
    style=("green", "Qwen"),
    capabilities=["code_editing", "bug_investigation", "general_coding"],
    preferred_task_types=["code_edit", "bug_investigation", "general"],
    avoid_task_types=["architecture"],
    supports_tools=False,
    supports_code_editing=True,
    supports_long_context=False,
)
register(plugin)
