"""Gerenciamento de sinal SIGINT e término de grupo de processos."""
import os
import signal
import threading


def terminate_process_group(proc) -> None:
    """Termina o processo e todo seu grupo (filhos)."""
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass


class EscMonitor:
    """Monitora Ctrl+C (SIGINT) e sinaliza via cancel_event."""

    def __init__(self, cancel_event: threading.Event):
        self._cancel_event = cancel_event
        self._old_handler = None

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
            except Exception:
                pass
            self._old_handler = None
