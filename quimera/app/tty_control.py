"""TTY control helpers for echo suppression."""

import sys


class TtyController:
    """Gerencia flags de terminal (ECHOCTL) para suppress/restore."""

    def __init__(self):
        self._echoctl_fd = None
        self._echoctl_attrs = None

    def suppress_control_echo(self) -> None:
        """Desativa eco visual de controles (^C/^Z) enquanto o chat está ativo."""
        stdin = getattr(sys, "stdin", None)
        if stdin is None or not getattr(stdin, "isatty", lambda: False)():
            return
        try:
            import termios  # pylint: disable=import-outside-toplevel
        except Exception:
            return
        if not hasattr(termios, "ECHOCTL"):
            return
        try:
            fd = stdin.fileno()
            attrs = termios.tcgetattr(fd)
        except Exception:
            return
        lflag = attrs[3]
        if (lflag & termios.ECHOCTL) == 0:
            return
        updated = list(attrs)
        updated[3] = lflag & ~termios.ECHOCTL
        try:
            termios.tcsetattr(fd, termios.TCSANOW, updated)
        except Exception:
            return
        self._echoctl_fd = fd
        self._echoctl_attrs = attrs

    def restore_control_echo(self) -> None:
        """Restaura flags de TTY alteradas por suppress_control_echo()."""
        fd = self._echoctl_fd
        attrs = self._echoctl_attrs
        if fd is None or attrs is None:
            return
        try:
            import termios  # pylint: disable=import-outside-toplevel
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass
        finally:
            self._echoctl_fd = None
            self._echoctl_attrs = None
