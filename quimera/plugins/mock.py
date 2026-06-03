"""Plugin mock usado pelos testes de registro de plugins."""

from quimera.plugins.base import AgentPlugin, register


plugin = AgentPlugin(
    name="mock",
    prefix="/mock",
    icon="🧪",
    cmd=["echo"],
    prompt_as_arg=True,
    style=("white", "Mock"),
    capabilities=["documentation"],
    preferred_task_types=["general"],
    supports_tools=False,
    supports_code_editing=False,
    supports_long_context=False,
    supports_task_execution=True,
    supports_warm_pool=False,
    base_tier=1,
)
register(plugin)
