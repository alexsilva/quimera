"""Barramento de eventos de domínio com suporte a subscribe/unsubscribe."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .task_events import TaskEvent


Handler = Callable[[TaskEvent], None]
"""Assinatura de handler: recebe um evento e processa."""


class EventSink:
    """Barramento thread-safe de eventos de domínio.

    Permite subscribe/unsubscribe com suporte a hierarquia de tipos
    (handlers registrados para TaskEvent recebem todas as subclasses).
    Exceções em handlers são isoladas (não afetam outros handlers).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[type[TaskEvent], list[Handler]] = {}

    def subscribe(self, event_type: type[TaskEvent], handler: Handler) -> Callable[[], None]:
        """Registra handler para um tipo de evento. Retorna função de unsubscribe."""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                handlers = self._handlers.get(event_type)
                if handlers and handler in handlers:
                    handlers.remove(handler)

        return _unsubscribe

    def publish(self, event: TaskEvent) -> None:
        """Publica evento para todos os handlers compatíveis (incluindo supertipos)."""
        with self._lock:
            matched: list[Handler] = []
            for ev_type, ev_handlers in self._handlers.items():
                if isinstance(event, ev_type):
                    matched.extend(ev_handlers)

        for h in matched:
            try:
                h(event)
            except Exception:
                pass

    def clear(self) -> None:
        """Remove todos os handlers."""
        with self._lock:
            self._handlers.clear()
