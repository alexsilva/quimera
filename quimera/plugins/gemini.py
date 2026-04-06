from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="gemini",
    prefix="/gemini",
    cmd=["gemini", "--approval-mode=yolo", "-p"],
    style=("cyan", "Gemini"),
    prompt_as_arg=True,
    capabilities=["code_review", "documentation", "general_reasoning", "code_editing", "complex_refactoring", "multimodal_analysis"],
    preferred_task_types=["code_review", "documentation", "general", "code_edit", "architecture", "bug_investigation"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=True, base_tier=3,
)
register(plugin)
