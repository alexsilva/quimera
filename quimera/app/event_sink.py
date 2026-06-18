"""Barramento de eventos de domínio com suporte a subscribe/unsubscribe."""
from __future__ import annotations

import queue as _queue_module
import threading
from collections.abc import Callable

from ..tasks.events import TaskEvent


Handler = Callable[[TaskEvent], None]
"""Assinatura de handler: recebe um evento e processa."""


class EventSink:
    """Barramento thread-safe de eventos de domínio.

    Permite subscribe/unsubscribe com suporte a hierarquia de tipos
    (handlers registrados para TaskEvent recebem todas as subclasses).
    Exceções em handlers são isoladas (não afetam outros handlers).
    """

    def __init__(self, ui_queue: "_queue_module.Queue | None" = None) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[type[TaskEvent], list[Handler]] = {}
        self._ui_queue = ui_queue
        self._pending_events: "_queue_module.Queue[TaskEvent]" = _queue_module.Queue()

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
        """Publica evento; fora da main thread, apenas enfileira para consumo posterior."""
        if threading.current_thread() is not threading.main_thread():
            self._pending_events.put(event)
            return
        self._dispatch(event)

    def drain_pending(self) -> None:
        """Despacha na main thread todos os eventos publicados por threads auxiliares."""
        while True:
            try:
                event = self._pending_events.get_nowait()
            except _queue_module.Empty:
                break
            try:
                self._dispatch(event)
            finally:
                self._pending_events.task_done()

    def _dispatch(self, event: TaskEvent) -> None:
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
        while True:
            try:
                self._pending_events.get_nowait()
            except _queue_module.Empty:
                break
            else:
                self._pending_events.task_done()
