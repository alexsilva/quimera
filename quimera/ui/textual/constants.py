"""Constantes compartilhadas da interface Textual."""
from __future__ import annotations

NO_RESPONSE_MESSAGE = "sem resposta válida"
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
