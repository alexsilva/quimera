"""Container de estado de runtime para QuimeraApp.

Encapsula locks, filas e contadores que antes viviam
diretamente em ``QuimeraApp.__init__``, reduzindo o monólito.
"""

import queue
import threading


class AppRuntimeState:
    """Estado mutável de runtime: input não-bloqueante e controle de inflight do chat."""

    def __init__(self) -> None:
        # ── nonblocking input ──────────────────────────────────────────
        self.nonblocking_prompt_visible = False
        self.nonblocking_prompt_text = ""
        self.nonblocking_input_thread: threading.Thread | None = None
        self.nonblocking_input_queue: queue.Queue | None = None
        self.nonblocking_input_status = "idle"
        self.nonblocking_input_status_lock = threading.Lock()
        self.prompt_owning_thread_id: int | None = None

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

    # ── setters for use as bound-method callbacks ──────────────────────

    def set_input_status(self, v: str) -> None:
        self.nonblocking_input_status = v

    def set_prompt_text(self, v: str) -> None:
        self.nonblocking_prompt_text = v

    def set_prompt_owner(self, v: int | None) -> None:
        self.prompt_owning_thread_id = v

    def set_prompt_visible(self, v: bool) -> None:
        self.nonblocking_prompt_visible = v

    def release_chat_slot(self) -> None:
        slot = getattr(self, "chat_slot_semaphore", None)
        if slot is not None:
            slot.release()


def _run(fn):
    if fn is not None:
        fn()
