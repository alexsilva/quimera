from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="qwen",
    prefix="/qwen",
    style=("green", "Qwen"),
    driver="openai_compat",
    model="qwen3-coder:30b",
    base_url="http://localhost:11434/v1",
    # api_key_env não necessário para Ollama local
    capabilities=["code_review", "code_editing"],
    preferred_task_types=["code_review", "code_edit"],
    avoid_task_types=[],
    supports_tools=True,
    supports_code_editing=True,
    supports_long_context=False,
    supports_task_execution=True,
    base_tier=1,
)
register(plugin)
