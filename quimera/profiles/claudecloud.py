"""Componentes de `quimera.profiles.claudecloud`.

Perfil `claudecloud`: template de execução para modelos da Anthropic direto
na Messages API com a subscription logada no Claude Code
(`~/.claude/.credentials.json`), sem nunca executar o binário `claude`.
O tool calling roda no ToolExecutor do Quimera, então o agente enxerga
exclusivamente as ferramentas do Quimera.

O perfil não define modelo padrão e não executa sozinho: só executa via
conexão explícita (`/connect <nome>` com perfil de execução `claudecloud`
e modelo informado, ex. `/connect claudecloud-sonnet`).
"""
from __future__ import annotations

import os
from pathlib import Path

from quimera.profiles.base import ExecutionProfile, OpenAIConnection, register

# Mesmo endpoint usado pelo driver em quimera.runtime.drivers.claudecloud;
# duplicado aqui para não importar httpx na carga dos profiles.
CLAUDE_CLOUD_BASE_URL = "https://api.anthropic.com"

# Timeout de leitura entre eventos SSE; thinking longo pode ficar períodos
# sem emitir texto.
_REQUEST_TIMEOUT_SECONDS = 600.0


def _claude_home() -> str:
    """Diretório do Claude Code; espelha claude_auth.default_claude_home sem
    importar o módulo (que puxa httpx) na carga dos profiles."""
    for env in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"):
        override = (os.environ.get(env) or "").strip()
        if override:
            return override
    return str(Path.home() / ".claude")


class ClaudeCloudProfile(ExecutionProfile):
    """Template de execução Claude Cloud (Messages API via conta do Claude Code)."""

    def effective_connection(self) -> OpenAIConnection | None:
        """Retorna somente a conexão nomeada já configurada.

        O registro estático ``claudecloud`` é um template, portanto não
        fabrica modelo nem conexão implícita. Instâncias dinâmicas herdadas
        recebem ``_connection_override`` pelo fluxo de ``/connect``.
        """
        if self._connection_override is not None:
            return self._connection_override
        return None

    def configure_with_model(self, model_id: str) -> OpenAIConnection:
        """Retorna conexão API com o model_id informado, sem exigir padrão."""
        normalized = (model_id or "").strip()
        if not normalized:
            raise ValueError("model_id não pode ser vazio.")
        return OpenAIConnection(
            model=normalized,
            base_url=self.base_url or CLAUDE_CLOUD_BASE_URL,
            api_key_env="",
            provider="claudecloud",
            supports_native_tools=True,
            extra_body=None,
            request_timeout=_REQUEST_TIMEOUT_SECONDS,
        )


register(ClaudeCloudProfile(
    name="claudecloud",
    prefix="/claudecloud",
    icon="☁️",
    style=("magenta", "Claude Cloud"),
    driver="claudecloud",
    base_url=CLAUDE_CLOUD_BASE_URL,
    runtime_rw_paths=[_claude_home()],
    capabilities=["architecture", "code_review", "planning", "documentation", "code_editing", "tool_use"],
    preferred_task_types=["architecture", "code_review", "documentation", "code_edit", "general"],
    supports_tools=True,
    has_builtin_tools=False,
    tool_use_reliability="high",
    supports_code_editing=True,
    supports_long_context=True,
    supports_warm_pool=False,
    base_tier=3,
))
