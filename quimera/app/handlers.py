"""Componentes de `quimera.app.handlers`."""
import logging
import re
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class AppCallbacks:
    """Callbacks e estado necessário para o handler."""
    output_lock: "logging._LockType | None"
    redisplay_prompt: "Callable[[bool], None]"
    show_error: "Callable[[str], None]"
    show_warning: "Callable[[str], None]"
    show_system: "Callable[[str], None]"
    is_reading: "Callable[[], bool | str | None]"
    show_muted: "Callable[[str], None] | None" = None
    debug_enabled: "Callable[[], bool] | None" = None


class PromptAwareStderrHandler(logging.StreamHandler):
    """Clear and redraw the interactive prompt around staging logs."""

    def __init__(self, stream=None):
        """Inicializa uma instância de PromptAwareStderrHandler."""
        super().__init__(stream or sys.stderr)
        self._callbacks: AppCallbacks | None = None
        self._app = None
        self._early_buffer: list[logging.LogRecord] = []

    def bind_callbacks(
            self,
            *,
            output_lock: "logging._LockType | None",
            redisplay_prompt: "Callable[[bool], None]",
            show_error: "Callable[[str], None]",
            show_warning: "Callable[[str], None]",
            show_system: "Callable[[str], None]",
            show_muted: "Callable[[str], None] | None" = None,
            is_reading: "Callable[[], bool | str | None]" = lambda: False,
            debug_enabled: "Callable[[], bool] | None" = None,
    ) -> None:
        """Executa bind de callbacks."""
        self._callbacks = AppCallbacks(
            output_lock=output_lock,
            redisplay_prompt=redisplay_prompt,
            show_error=show_error,
            show_warning=show_warning,
            show_system=show_system,
            show_muted=show_muted,
            is_reading=is_reading,
            debug_enabled=debug_enabled,
        )
        buffered, self._early_buffer = self._early_buffer, []
        for record in buffered:
            self.emit(record)

    def emit(self, record):
        """Executa emit."""
        callbacks = self._callbacks

        if record.levelno < logging.WARNING:
            # INFO/DEBUG → arquivo apenas; excepção: MCP server em debug mode (muted)
            if callbacks is not None:
                debug_enabled = False
                if callable(getattr(callbacks, "debug_enabled", None)):
                    try:
                        debug_enabled = bool(callbacks.debug_enabled())
                    except Exception:
                        pass
                if debug_enabled and record.name == "quimera.runtime.mcp.server":
                    if callable(callbacks.show_muted):
                        callbacks.show_muted(_friendly_message(record))
            return

        # WARNING/ERROR → chat com formato amigável
        if callbacks is None:
            # callbacks ainda não configurados: bufferizar para exibir no feed quando a UI iniciar
            self._early_buffer.append(record)
            return

        message = _friendly_message(record)

        if record.levelno >= logging.ERROR:
            if callable(callbacks.show_error):
                callbacks.show_error(message)
                return
        if callable(callbacks.show_warning):
            callbacks.show_warning(message)

    def drain_to_stderr(self) -> None:
        """Drena buffer inicial para stderr (fallback quando bind_callbacks nunca foi chamado)."""
        buffered, self._early_buffer = self._early_buffer, []
        for record in buffered:
            try:
                super().emit(record)
            except Exception:
                pass


_AGENT_QUOTED = re.compile(r'"([^"]+)"')

_FRIENDLY_LABELS: dict[str, str] = {
    "agent_call_service": "",  # tratado separadamente — extrai nome do agente da mensagem
}


def _friendly_message(record: logging.LogRecord) -> str:
    component = record.module or record.name.split(".")[-1]
    label = _FRIENDLY_LABELS.get(component, component)
    msg = record.getMessage()
    if len(msg) > 100:
        msg = msg[:97] + "..."

    # agent_call_service: extrai o nome do agente entre aspas e usa como prefixo
    if component == "agent_call_service":
        m = _AGENT_QUOTED.search(msg)
        if m:
            agent_name = m.group(1)
            clean = _AGENT_QUOTED.sub("", msg).strip()
            clean = re.sub(r'\s+', ' ', clean)
            clean = re.sub(r'\s+(?:from|for|with)\s+', ' ', clean, count=1)
            clean = re.sub(r'\s+', ' ', clean)
            clean = clean.strip(' ,')
            clean = re.sub(r'\s*,\s*', ', ', clean)
            clean = clean.strip()
            return f"{agent_name}: {clean}"

    if label:
        return f"{label}: {msg}"
    return msg


