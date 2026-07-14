"""Mensagens e formatters pt-BR compartilhados entre renderers e camada app.

Módulo deliberadamente sem dependências internas: é importado por
``quimera.ui.base`` (contrato de renderers) e pelos consumidores em
``quimera.app`` sem risco de ciclo com ``quimera.ui.textual``.
"""
from __future__ import annotations

NO_RESPONSE_MESSAGE = "sem resposta válida"

#: Rótulos visuais (pt-BR) para os motivos estruturados de nova tentativa.
#: Fonte única de tradução: chamadas estruturadas informam o ``reason``
#: canônico e o renderer o converte no texto exibido, sem parsing reverso.
RETRY_REASON_LABELS = {
    "no_response": "sem resposta",
    "invalid_response": "resposta inválida",
    "comm_error": "falha de comunicação",
}
FAILOVER_DEFAULT_MESSAGE = "não respondeu"


def format_retry_message(reason: str, attempt: int, limit: int, detail: str = "") -> str:
    """Formata frase pt-BR de nova tentativa para renderers sem canal estruturado.

    Usado só como fallback (ex.: TerminalRenderer legado); renderers ricos
    recebem os campos separados via ``notify_agent_retry``.
    """
    label = RETRY_REASON_LABELS.get(str(reason), str(reason))
    message = f"{label} · tentativa {int(attempt)}/{int(limit)}"
    detail = str(detail or "").strip()
    if detail:
        message = f"{message} · {detail}"
    return message


def format_failover_message(
    agent: str,
    target: str,
    message: str = FAILOVER_DEFAULT_MESSAGE,
) -> str:
    """Formata frase pt-BR de failover para renderers sem canal estruturado."""
    return f"{agent} {message}, continuando com {target}"
