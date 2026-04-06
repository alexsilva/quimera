from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="gemini",
    prefix="/gemini",
    cmd=["gemini", "--approval-mode=yolo", "-p"],
    style=("cyan", "Gemini"),
    prompt_as_arg=True,
    capabilities=["code_review", "documentation", "general_reasoning"],
    preferred_task_types=["code_review", "documentation", "general"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=False,
    supports_long_context=True,
)
register(plugin)
