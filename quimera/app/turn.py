"""Controle de turno do chat humano ↔ agente."""
import threading


class TurnManager:
    """Gerencia o turno de fala no diálogo humano ↔ agente."""

    def __init__(self):
        self._is_human_turn = True
        self._lock = threading.Lock()
        self._human_turn_event = threading.Event()
        self._human_turn_event.set()

    @property
    def is_human_turn(self) -> bool:
        with self._lock:
            return self._is_human_turn

    @property
    def is_ai_turn(self) -> bool:
        with self._lock:
            return not self._is_human_turn

    def next_turn(self) -> None:
        """Alterna o turno: humano <-> agente."""
        with self._lock:
            self._is_human_turn = not self._is_human_turn
            if self._is_human_turn:
                self._human_turn_event.set()
            else:
                self._human_turn_event.clear()

    def reset(self) -> None:
        """Reseta para turno do humano."""
        with self._lock:
            self._is_human_turn = True
            self._human_turn_event.set()

    def wait_for_human_turn(self, timeout: float | None = None) -> bool:
        """Aguarda até o turno humano ficar disponível."""
        return self._human_turn_event.wait(timeout=timeout)
