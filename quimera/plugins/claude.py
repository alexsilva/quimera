from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="claude",
    prefix="/claude",
    cmd=["claude", "--permission-mode=dontAsk", "-p"],
    style=("blue", "Claude"),
    capabilities=["architecture", "code_review", "planning", "documentation"],
    preferred_task_types=["architecture", "code_review", "documentation"],
    avoid_task_types=["test_execution"],
    supports_tools=True,
    supports_code_editing=False,
    supports_long_context=True,
)
register(plugin)
