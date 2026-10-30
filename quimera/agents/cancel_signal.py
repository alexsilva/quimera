"""Sinal de cancelamento de um client isolado que também observa o da rodada."""
from __future__ import annotations

import threading
import time


class ForkCancelEvent(threading.Event):
    """Evento próprio de um fork que também responde ao evento da rodada.

    ``set`` e ``clear`` agem só no sinal próprio: parar um fork não cancela a
    rodada, e um fork que começa a executar (``EscMonitor.start`` limpa o
    evento) não apaga um Ctrl+C em curso. ``is_set`` responde ao sinal próprio
    ou ao da rodada, então o cancelamento da rodada continua alcançando todos
    os forks sem registro nem propagação.
    """

    _POLL_SECONDS = 0.05

    def __init__(self, round_event: threading.Event) -> None:
        super().__init__()
        self._round_event = round_event

    @property
    def round_event(self) -> threading.Event:
        """Evento da rodada observado por este fork."""
        return self._round_event

    def is_set(self) -> bool:
        return super().is_set() or self._round_event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Espera em fatias curtas: o sinal da rodada não acorda este evento."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            super().wait(self._POLL_SECONDS if remaining is None else min(self._POLL_SECONDS, remaining))
        return True
