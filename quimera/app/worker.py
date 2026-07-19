"""ChatWorker — thread de processamento resiliente."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from .render_event import RenderEvent

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 2.0   # segundos entre heartbeats


@dataclass(frozen=True)
class ChatWorkItem:
    """Mensagem do chat com indicação de slot previamente reservado."""

    message: object
    slot_reserved: bool = True


class ChatWorker:
    """Worker thread que processa mensagens do chat de forma resiliente.

    - Sobrevive a exceções: captura, loga, enfileira RenderEvent("error"), restaura turno.
    - Emite heartbeat periódico para que o main loop possa detectar inatividade.
    - Nunca chama renderer diretamente — toda UI passa por ui_queue.
    """

    def __init__(
        self,
        chat_queue: queue.Queue,
        ui_event_queue: queue.Queue,
        agent_executor,
        turn_manager,
    ) -> None:
        self._chat_queue = chat_queue
        self._ui_queue = ui_event_queue
        self._agent_executor = agent_executor
        self._turn_manager = turn_manager
        self._thread: threading.Thread | None = None
        self._last_heartbeat = time.monotonic()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="quimera-chat-worker"
        )
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        while True:
            try:
                msg = self._chat_queue.get(timeout=HEARTBEAT_INTERVAL)
            except queue.Empty:
                self._emit_heartbeat()
                continue
            try:
                if msg is None:
                    return   # sinal de shutdown
                self._last_heartbeat = time.monotonic()
                self._process_chat_queue(msg)
            finally:
                self._chat_queue.task_done()

    def _process_chat_queue(self, msg) -> None:
        """Processa uma mensagem sem derrubar a thread em caso de erro."""
        try:
            if isinstance(msg, ChatWorkItem):
                self._agent_executor(
                    msg.message,
                    slot_reserved=msg.slot_reserved,
                )
            else:
                self._agent_executor(msg)
        except Exception as exc:
            logger.exception("ChatWorker: erro ao processar mensagem")
            self._ui_queue.put(RenderEvent(RenderEvent.ERROR, str(exc)))
            try:
                self._turn_manager.reset()
            except Exception:
                logger.exception("ChatWorker: falha ao restaurar turno após erro")

    def _emit_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            self._ui_queue.put(RenderEvent(RenderEvent.HEARTBEAT, ""))
