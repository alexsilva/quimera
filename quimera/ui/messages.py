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


def format_duration(duration_ms: int | None) -> str:
    """Formata duração em ms para exibição compacta (``850ms`` / ``2.3s``)."""
    if not isinstance(duration_ms, int) or duration_ms < 0:
        return "n/a"
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"


def format_turn_summary_lines(detail: dict) -> list[str]:
    """Resumo textual do turno (tools executadas) para renderers sem painel próprio."""
    tools = detail.get("tools") if isinstance(detail, dict) else None
    if not isinstance(tools, list) or not tools:
        return []
    total = len([tool for tool in tools if isinstance(tool, dict)])
    ok_count = 0
    err_count = 0
    total_ms = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        status = str(tool.get("status") or "").lower()
        if status in {"ok", "success", "succeeded"}:
            ok_count += 1
        if status in {"error", "failed", "fail", "timeout"}:
            err_count += 1
        duration_ms = tool.get("duration_ms")
        if isinstance(duration_ms, int) and duration_ms >= 0:
            total_ms += duration_ms
    total_duration = format_duration(total_ms)
    return [f"TOOLS: {total} chamadas · {ok_count} ok · {err_count} erro · {total_duration}"]
