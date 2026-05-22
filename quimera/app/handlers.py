"""Componentes de `quimera.app.handlers`."""
import logging
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
    is_reading: "Callable[[], str | None]"


class PromptAwareStderrHandler(logging.StreamHandler):
    """Clear and redraw the interactive prompt around staging logs."""

    def __init__(self, stream=None):
        """Inicializa uma instância de PromptAwareStderrHandler."""
        super().__init__(stream or sys.stderr)
        self._callbacks: AppCallbacks | None = None
        self._app = None

    def bind_app(self, app) -> None:
        """Compat shim para testes e call sites legados.

        Mantém o contrato antigo sem reintroduzir dependência forte no fluxo novo:
        o handler continua operando sobre callbacks, apenas materializados a partir
        de um objeto app-like.
        """
        self._app = app
        if app is None:
            self._callbacks = None
            return

        def _noop(*_args, **_kwargs):
            return None

        self.bind_callbacks(
            output_lock=getattr(app, "_output_lock", None),
            redisplay_prompt=getattr(app, "_redisplay_user_prompt_if_needed", _noop),
            show_error=getattr(app, "show_error_message", _noop),
            show_warning=getattr(app, "show_warning_message", _noop),
            show_system=getattr(app, "show_system_message", _noop),
            is_reading=lambda: getattr(app, "_nonblocking_input_status", None),
        )

    def bind_callbacks(
            self,
            *,
            output_lock: "logging._LockType | None",
            redisplay_prompt: "Callable[[bool], None]",
            show_error: "Callable[[str], None]",
            show_warning: "Callable[[str], None]",
            show_system: "Callable[[str], None]",
            is_reading: "Callable[[], str | None]"
    ) -> None:
        """Executa bind de callbacks."""
        self._callbacks = AppCallbacks(
            output_lock=output_lock,
            redisplay_prompt=redisplay_prompt,
            show_error=show_error,
            show_warning=show_warning,
            show_system=show_system,
            is_reading=is_reading
        )

    def emit(self, record):
        """Executa emit."""
        callbacks = self._callbacks
        if callbacks is None:
            super().emit(record)
            return

        if callbacks.is_reading() == "reading" and record.levelno < logging.WARNING:
            return

        formatter = self.formatter or logging.Formatter("%(message)s")
        message = formatter.format(record)

        _raw = str(record.msg) if record.msg else ""
        _is_operational_noise = _raw.startswith("[DISPATCH]") or _raw.startswith("[GATEWAY]")

        if record.levelno >= logging.ERROR:
            if callable(callbacks.show_error):
                callbacks.show_error(message)
                return
        if record.levelno >= logging.WARNING:
            if callable(callbacks.show_warning):
                callbacks.show_warning(message)
                return
        if _is_operational_noise:
            super().emit(record)
            return

        if callable(callbacks.show_system):
            callbacks.show_system(message)
            return

        stdin_is_tty = sys.stdin is not None and sys.stdin.isatty()
        if stdin_is_tty and self.stream is sys.stderr:
            self.stream = sys.stdout

        with callbacks.output_lock:
            super().emit(record)
            self.flush()
            callbacks.redisplay_prompt(clear_first=False)
