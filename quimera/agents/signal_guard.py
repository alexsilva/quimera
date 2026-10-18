"""Gerenciamento de sinal SIGINT e término de grupo de processos."""
import logging
import os
import signal
import threading
from types import FrameType
from typing import Any, Callable

_logger = logging.getLogger(__name__)


def terminate_process_group(proc) -> None:
    """Termina um grupo isolado; nunca sinaliza o grupo compartilhado do app."""
    try:
        pid = int(proc.pid)
        pgid = os.getpgid(pid)
        current_pgid = os.getpgrp()
        if pgid == pid and pgid != current_pgid:
            os.killpg(pgid, signal.SIGTERM)
            return
    except (OSError, TypeError, ValueError):
        pass
    try:
        proc.terminate()
    except OSError:
        pass


class EscMonitor:
    """Monitora Ctrl+C (SIGINT) e sinaliza via cancel_event."""

    def __init__(self, cancel_event: threading.Event):
        self._cancel_event = cancel_event
        self._old_handler: Callable[[int, FrameType | None], Any] | int | None = None

    def start(self) -> None:
        self._cancel_event.clear()
        if threading.current_thread() is not threading.main_thread():
            self._old_handler = None
            return

        def _handler(signum, frame):
            if signum == signal.SIGINT:
                self._cancel_event.set()

        self._old_handler = signal.signal(signal.SIGINT, _handler)

    def stop(self) -> None:
        if self._old_handler is not None:
            try:
                signal.signal(signal.SIGINT, self._old_handler)
            except (ValueError, OSError, TypeError) as exc:
                _logger.warning("SignalGuard: falha ao restaurar o handler de SIGINT: %s", exc)
            self._old_handler = None
