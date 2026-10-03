"""Componentes de `quimera.app.handlers`."""
import logging
import sys


class PromptAwareStderrHandler(logging.StreamHandler):
    """Segura os WARNING+ do logger de tela até a UI assumir o terminal.

    Enquanto a UI não está ativa (boot, ou falha antes de ela subir) os
    registros ficam em buffer e saem no stderr via `drain_to_stderr`, depois
    que a tela alternativa é restaurada. Com a UI ativa nada é escrito: o
    arquivo de log guarda todos os registros e o chat recebe apenas as
    notificações explícitas (`show_*`/`notify_*` da camada de sistema).
    """

    def __init__(self, stream=None):
        """Inicializa uma instância de PromptAwareStderrHandler."""
        super().__init__(stream or sys.stderr)
        self._ui_active = False
        self._early_buffer: list[logging.LogRecord] = []

    def mark_ui_active(self) -> None:
        """Sinaliza que a UI assumiu o terminal; registros novos ficam só no arquivo."""
        self._ui_active = True

    def emit(self, record):
        """Guarda WARNING+ emitidos antes da UI; ignora o resto."""
        if self._ui_active or record.levelno < logging.WARNING:
            return
        self._early_buffer.append(record)

    def drain_to_stderr(self) -> None:
        """Imprime no stderr os registros acumulados antes de a UI subir."""
        buffered, self._early_buffer = self._early_buffer, []
        for record in buffered:
            try:
                super().emit(record)
            except Exception:
                pass
