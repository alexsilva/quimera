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

        stdin_is_tty = sys.stdin is not None and sys.stdin.isatty()
        if stdin_is_tty and self.stream is sys.stderr:
            self.stream = sys.stdout

        with app._output_lock:
            app._clear_user_prompt_line_if_needed()
            super().emit(record)
            self.flush()
            app._redisplay_user_prompt_if_needed()
