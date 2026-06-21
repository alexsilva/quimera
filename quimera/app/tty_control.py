"""TTY control helpers.

O uso direto de ``termios``/``tty`` (raw mode) foi banido do projeto por causar
travamentos de input/shell em modo threads. Esta classe permanece como no-op
para preservar a interface usada por ``chat_processor`` e pelos testes; a
supressão de eco de controles (^C/^Z) deixa de ser aplicada.
"""


class TtyController:
    """No-op: a supressão de eco via termios foi banida. Mantida por compatibilidade."""

    def __init__(self):
        self._echoctl_fd = None
        self._echoctl_attrs = None

    def suppress_control_echo(self) -> None:
        """No-op — termios banido; nenhuma flag de terminal é alterada."""
        return

    def restore_control_echo(self) -> None:
        """No-op — nada a restaurar, pois nada é suprimido."""
        return
