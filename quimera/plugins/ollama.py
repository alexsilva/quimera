"""Registra plugins baseados em Ollama via driver compatível com OpenAI."""
from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="ollama-granite4",
    prefix="/ollama-granite4",
    icon="⚡",
    style=("granite4", "OllamaGranite"),
    driver="openai_compat",
    model="granite4.1:8b",
    base_url="http://localhost:11434/v1",
    # api_key_env não necessário para Ollama local
    capabilities=["code_review", "code_editing"],
    preferred_task_types=["code_review", "code_edit"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="low",
    supports_code_editing=True,
    supports_long_context=False,
    supports_task_execution=True,
    base_tier=1,
)
register(plugin)
