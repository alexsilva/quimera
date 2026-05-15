"""Componentes de `quimera.app.handlers`."""
import logging
import sys


class PromptAwareStderrHandler(logging.StreamHandler):
    """Clear and redraw the interactive prompt around staging logs."""

    def __init__(self, stream=None):
        """Inicializa uma instância de PromptAwareStderrHandler."""
        super().__init__(stream or sys.stderr)
        self.app = None
        self._app = None

    def bind_app(self, app) -> None:
        """Executa bind app."""
        self.app = app
        self._app = app

    def emit(self, record):
        """Executa emit."""
        app = self.app
        if app is None:
            super().emit(record)
            return

        # INFO/DEBUG internos geram churn no TTY quando o prompt não bloqueante
        # está ativo. Mantemos warnings e errors visíveis.
        if (
                getattr(app, "_nonblocking_input_status", None) == "reading"
                and record.levelno < logging.WARNING
        ):
            return

        formatter = self.formatter or logging.Formatter("%(message)s")
        message = formatter.format(record)

        _raw = str(record.msg) if record.msg else ""
        _is_operational_noise = _raw.startswith("[DISPATCH]") or _raw.startswith("[GATEWAY]")

        if record.levelno >= logging.ERROR:
            show_error_message = getattr(app, "show_error_message", None)
            if callable(show_error_message):
                show_error_message(message)
                return
        if record.levelno >= logging.WARNING:
            show_warning_message = getattr(app, "show_warning_message", None)
            if callable(show_warning_message):
                show_warning_message(message)
                return
        if _is_operational_noise:
            super().emit(record)
            return

        show_system_message = getattr(app, "show_system_message", None)
        if callable(show_system_message):
            show_system_message(message)
            return

        stdin_is_tty = sys.stdin is not None and sys.stdin.isatty()
        if stdin_is_tty and self.stream is sys.stderr:
            self.stream = sys.stdout

        with app._output_lock:
            app._clear_user_prompt_line_if_needed()
            super().emit(record)
            self.flush()
            app._redisplay_user_prompt_if_needed(clear_first=False)
