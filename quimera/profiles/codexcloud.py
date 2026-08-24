"""Componentes de `quimera.profiles.codexcloud`.

Perfil `codexcloud`: executa modelos do Codex direto no backend da OpenAI
(API Responses) com a mesma conta logada no Codex CLI (`~/.codex/auth.json`),
sem nunca executar o binário `codex`. O tool calling roda no ToolExecutor do
Quimera, então o agente enxerga exclusivamente as ferramentas do Quimera.
"""
from __future__ import annotations

import logging
import tomllib
from functools import lru_cache
from pathlib import Path

from quimera.profiles.base import ExecutionProfile, OpenAIConnection, register

_logger = logging.getLogger(__name__)

# Mesmo endpoint usado pelo driver em quimera.runtime.drivers.codexcloud;
# duplicado aqui para não importar httpx/openai na carga dos profiles.
CODEX_CLOUD_BASE_URL = "https://chatgpt.com/backend-api/codex"

_FALLBACK_MODEL = "gpt-5.5"
_FALLBACK_REASONING_EFFORT = "medium"

# Timeout de leitura entre eventos SSE; reasoning alto pode ficar longos
# períodos sem emitir texto.
_REQUEST_TIMEOUT_SECONDS = 600.0


@lru_cache(maxsize=1)
def _codex_config_defaults() -> tuple[str, str]:
    """Lê modelo e reasoning effort do config.toml do Codex CLI."""
    config_path = Path.home() / ".codex" / "config.toml"
    model = _FALLBACK_MODEL
    effort = _FALLBACK_REASONING_EFFORT
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _logger.debug("codexcloud: config.toml do Codex indisponível (%s); usando defaults", exc)
        return model, effort
    raw_model = str(config.get("model") or "").strip()
    if raw_model:
        model = raw_model
    raw_effort = str(config.get("model_reasoning_effort") or "").strip().lower()
    if raw_effort in {"minimal", "low", "medium", "high", "xhigh"}:
        effort = raw_effort
    return model, effort


class CodexCloudProfile(ExecutionProfile):
    """Profile do Codex Cloud (backend Codex via conta ChatGPT do Codex CLI)."""

    def effective_connection(self) -> OpenAIConnection:
        """Retorna conexão API espelhando modelo/effort do config do Codex CLI."""
        if self._connection_override is not None:
            return self._connection_override
        default_model, reasoning_effort = _codex_config_defaults()
        return OpenAIConnection(
            model=self.model or default_model,
            base_url=self.base_url or CODEX_CLOUD_BASE_URL,
            api_key_env="",
            provider="codexcloud",
            supports_native_tools=True,
            extra_body={"reasoning": {"effort": reasoning_effort, "summary": "auto"}},
            request_timeout=_REQUEST_TIMEOUT_SECONDS,
        )


register(CodexCloudProfile(
    name="codexcloud",
    prefix="/codexcloud",
    icon="☁️",
    style=("cyan", "Codex Cloud"),
    driver="codexcloud",
    base_url=CODEX_CLOUD_BASE_URL,
    runtime_rw_paths=[str(Path.home() / ".codex")],
    capabilities=["code_editing", "code_review", "test_execution", "bug_investigation", "tool_use"],
    preferred_task_types=["code_edit", "code_review", "test_execution", "bug_investigation", "general"],
    supports_tools=True,
    has_builtin_tools=False,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    supports_warm_pool=False,
    base_tier=2,
))
