"""Registra plugins baseados em Ollama via driver compatível com OpenAI."""
from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="ollama-qwen",
    prefix="/ollama-qwen",
    style=("green", "OllamaQwen"),
    driver="openai_compat",
    model="qwen3-coder:30b",
    base_url="http://localhost:11434/v1",
    # api_key_env não necessário para Ollama local
    capabilities=["code_review", "code_editing"],
    preferred_task_types=["code_review", "code_edit"],
    avoid_task_types=[],
    supports_tools=True,
    tool_use_reliability="low",
    supports_code_editing=True,
    supports_long_context=False,
    supports_task_execution=True,
    base_tier=1,
)
register(plugin)

plugin = AgentPlugin(
    name="ollama-gpt-oss",
    prefix="/ollama-gpt-oss",
    style=("cyan", "OllamaGptOss"),
    driver="openai_compat",
    model="gpt-oss:20b",
    base_url="http://localhost:11434/v1",
    # api_key_env não necessário para Ollama local
    capabilities=["code_review", "code_editing"],
    preferred_task_types=["code_review", "code_edit"],
    avoid_task_types=[],
    supports_tools=True,
    tool_use_reliability="medium",
    supports_code_editing=True,
    supports_long_context=False,
    supports_task_execution=True,
    base_tier=1,
)
register(plugin)

plugin = AgentPlugin(
    name="ollama-gemma4",
    prefix="/ollama-gemma4",
    style=("green", "OllamaGemma4"),
    driver="openai_compat",
    model="gemma4",
    base_url="http://localhost:11434/v1",
    # api_key_env não necessário para Ollama local
    capabilities=["code_review", "code_editing"],
    preferred_task_types=["code_review", "code_edit"],
    avoid_task_types=[],
    supports_tools=True,
    tool_use_reliability="medium",
    supports_code_editing=True,
    supports_long_context=False,
    supports_task_execution=True,
    base_tier=1,
)
register(plugin)
