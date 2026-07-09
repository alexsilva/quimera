"""Constantes compartilhadas da interface Textual."""
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
SUMMARY_SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
APPROVAL_TITLE = "Permissão solicitada"
APPROVAL_OPTIONS = (
    "s/sim/y/yes = aprovar",
    "n/não/no/enter = negar",
    "a/all/todas = aprovar todas",
)
TERMINAL_MODE_RESET = (
    "\x1b[?1000l"  # mouse click tracking
    "\x1b[?1002l"  # mouse button-event tracking
    "\x1b[?1003l"  # any-event mouse tracking
    "\x1b[?1005l"  # UTF-8 mouse mode
    "\x1b[?1006l"  # SGR mouse mode
    "\x1b[?1015l"  # urxvt mouse mode
    "\x1b[?2004l"  # bracketed paste
    "\x1b[?25h"    # cursor visible
)
