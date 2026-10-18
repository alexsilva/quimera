"""Container de estado de runtime para QuimeraApp.

Encapsula locks, filas e contadores que antes viviam
diretamente em ``QuimeraApp.__init__``, reduzindo o monólito.
"""

import threading


class AppRuntimeState:
    """Estado mutável de runtime: contadores de inflight/pendentes do chat e slots.

    O estado do prompt (ativo e thread dona) pertence ao ``InputGate``
    (``is_active``/``get_owner_thread_id``), não a este container.
    """

    def __init__(self) -> None:
        # ── chat inflight ──────────────────────────────────────────────
        self.chat_inflight_lock = threading.Lock()
        self.chat_inflight_count = 0
        self.chat_pending_count = 0
        self.chat_queue = None
        self.chat_executor = None
        self.chat_slot_semaphore = None

    # ── helpers inflight ───────────────────────────────────────────────

    def get_chat_inflight_count(self) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            return int(getattr(self, "chat_inflight_count", 0) or 0)
        with lock:
            return int(getattr(self, "chat_inflight_count", 0) or 0)

    def increment_chat_inflight(self, refresh_callback=None) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            current = int(getattr(self, "chat_inflight_count", 0) or 0) + 1
            self.chat_inflight_count = current
            _run(refresh_callback)
            return current
        with lock:
            current = int(getattr(self, "chat_inflight_count", 0) or 0) + 1
            self.chat_inflight_count = current
        _run(refresh_callback)
        return current

    def decrement_chat_inflight(self, refresh_callback=None) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            current = max(0, int(getattr(self, "chat_inflight_count", 0) or 0) - 1)
            self.chat_inflight_count = current
            _run(refresh_callback)
            return current
        with lock:
            current = max(0, int(getattr(self, "chat_inflight_count", 0) or 0) - 1)
            self.chat_inflight_count = current
        _run(refresh_callback)
        return current

    def get_chat_pending_count(self) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            return int(getattr(self, "chat_pending_count", 0) or 0)
        with lock:
            return int(getattr(self, "chat_pending_count", 0) or 0)

    def get_chat_outstanding_count(self) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            return (
                int(getattr(self, "chat_inflight_count", 0) or 0)
                + int(getattr(self, "chat_pending_count", 0) or 0)
            )
        with lock:
            return (
                int(getattr(self, "chat_inflight_count", 0) or 0)
                + int(getattr(self, "chat_pending_count", 0) or 0)
            )

    def increment_chat_pending(self, refresh_callback=None) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            current = int(getattr(self, "chat_pending_count", 0) or 0) + 1
            self.chat_pending_count = current
            _run(refresh_callback)
            return current
        with lock:
            current = int(getattr(self, "chat_pending_count", 0) or 0) + 1
            self.chat_pending_count = current
        _run(refresh_callback)
        return current

    def decrement_chat_pending(self, refresh_callback=None) -> int:
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            current = max(0, int(getattr(self, "chat_pending_count", 0) or 0) - 1)
            self.chat_pending_count = current
            _run(refresh_callback)
            return current
        with lock:
            current = max(0, int(getattr(self, "chat_pending_count", 0) or 0) - 1)
            self.chat_pending_count = current
        _run(refresh_callback)
        return current

    def promote_chat_pending_to_inflight(self, refresh_callback=None) -> tuple[int, int]:
        """Move um prompt da fila pendente para um slot ativo atomicamente."""
        lock = getattr(self, "chat_inflight_lock", None)
        if lock is None:
            pending = max(0, int(getattr(self, "chat_pending_count", 0) or 0) - 1)
            active = int(getattr(self, "chat_inflight_count", 0) or 0) + 1
            self.chat_pending_count = pending
            self.chat_inflight_count = active
            _run(refresh_callback)
            return active, pending
        with lock:
            pending = max(0, int(getattr(self, "chat_pending_count", 0) or 0) - 1)
            active = int(getattr(self, "chat_inflight_count", 0) or 0) + 1
            self.chat_pending_count = pending
            self.chat_inflight_count = active
        _run(refresh_callback)
        return active, pending

    def release_chat_slot(self) -> None:
        slot = getattr(self, "chat_slot_semaphore", None)
        if slot is not None:
            slot.release()


def _run(fn):
    if fn is not None:
        fn()
