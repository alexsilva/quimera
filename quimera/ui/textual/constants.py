"""Constantes compartilhadas da interface Textual."""
from __future__ import annotations

# Mensagens/formatters compartilhados vivem em quimera.ui.messages (módulo
# neutro, sem ciclo); re-exportados aqui por compatibilidade.
from quimera.ui.messages import (  # noqa: F401
    FAILOVER_DEFAULT_MESSAGE,
    NO_RESPONSE_MESSAGE,
    RETRY_REASON_LABELS,
    format_failover_message,
    format_retry_message,
)
SUMMARY_SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
SUMMARY_NOTIFICATION_MESSAGE = "Gerando resumo"
APPROVAL_TITLE = "Permissão solicitada"
APPROVAL_OPTIONS = (
    "y/sim = aprovar",
    "n/não = negar",
    "a/todas = aprovar todas",
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
