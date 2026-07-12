from __future__ import annotations

import threading
from typing import Callable


_Listener = Callable[[object | None, object | None], None]


class ExecutionModeState:
    """Observable holder for the current ExecutionMode.

    Consumers register a listener via ``on_change`` and are called whenever
    the mode changes.  This replaces ad-hoc getter lambdas (e.g.
    ``lambda: app.execution_mode``) and the manual propagation in
    ``_set_execution_mode``.
    """

    def __init__(self, initial: object | None = None) -> None:
        self._mode: object | None = initial
        self._lock = threading.RLock()
        self._listeners: list[_Listener] = []

    def get(self) -> object | None:
        with self._lock:
            return self._mode

    def set(self, mode: object | None) -> None:
        # Notifica incondicionalmente: setar o mesmo modo (inclusive None)
        # deve reaplicar efeitos derivados (ex.: policy.blocked_tools), como
        # fazia a propagação manual de _set_execution_mode.
        with self._lock:
            old = self._mode
            self._mode = mode
        self._notify(old, mode)

    def on_change(self, listener: _Listener) -> Callable[[], None]:
        """Register a listener; returns a callable to unregister."""
        with self._lock:
            self._listeners.append(listener)

        def _unregister() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return _unregister

    def _notify(self, old: object | None, new: object | None) -> None:
        for listener in list(self._listeners):
            try:
                listener(old, new)
            except Exception:
                pass
