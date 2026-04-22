"""Plugin para ChatGPT via API compatível com OpenAI local na porta 5000."""
from quimera.plugins.base import AgentPlugin, register

plugin = AgentPlugin(
    name="chatgpt",
    prefix="/chatgpt",
    icon="💬",
    style=("bright_yellow", "ChatGPT"),
    driver="openai_compat",
    model="gpt-4o",
    base_url="http://localhost:5532/v1",
    api_key_env="CHATGPT_API_KEY",  # Opcional, o driver usa 'ollama' como fallback se None/vazio
    capabilities=["code_review", "code_editing"],
    preferred_task_types=["code_review", "code_edit"],
    avoid_task_types=[],
    supports_tools=True,
    has_builtin_tools=True,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    supports_task_execution=True,
    base_tier=3,
)
register(plugin)
