"""Ciclo de vida do processamento de chat."""

from __future__ import annotations

import logging
import threading
import time

from .chat_round import ChatRoundContext


logger = logging.getLogger(__name__)

DEFAULT_CHAT_QUEUE_WAIT_TIMEOUT_SECONDS = 600.0


class ChatQueueWaitTimeoutError(TimeoutError):
    """A fila não disponibilizou capacidade dentro do limite operacional."""

    user_message = "A execução não conseguiu sair da fila dentro do limite de espera."


class ChatLifecycle:
    """Dono do fluxo de processamento, cancelamento e slots do chat."""

    def __init__(
        self,
        *,
        chat_round_orchestrator,
        system_layer,
        renderer,
        runtime_state,
        turn_manager,
        agent_client,
        ui_event_handler,
        session_services,
        task_services,
        session_state,
        dispatch_services,
        parse_routing,
        parse_response,
        refresh_parallel_toolbar,
        queue_wait_timeout_seconds: float = DEFAULT_CHAT_QUEUE_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._chat_round_orchestrator = chat_round_orchestrator
        self._system_layer = system_layer
        self._renderer = renderer
        self._runtime_state = runtime_state
        self._turn_manager = turn_manager
        self._agent_client = agent_client
        self._ui_event_handler = ui_event_handler
        self._session_services = session_services
        self._task_services = task_services
        self._session_state = session_state
        self._dispatch_services = dispatch_services
        self._parse_routing = parse_routing
        self._parse_response = parse_response
        self._ui_event_queue = None
        self._refresh_parallel_toolbar = refresh_parallel_toolbar
        self._queue_wait_timeout_seconds = max(
            0.0,
            float(queue_wait_timeout_seconds),
        )
        self._submission_futures: dict[int, tuple[object, str, bool]] = {}
        self._active_submission_ids: set[str] = set()
        self._cancelled_submission_ids: set[str] = set()
        self._running_submission_ids: set[str] = set()
        self._submission_lock = threading.RLock()
        self._processing_tls = threading.local()

    def bind_ui_event_queue(self, ui_event_queue) -> None:
        """Vincula a fila de eventos de UI materializada pelo loop de chat."""
        self._ui_event_queue = ui_event_queue

    def update_submission_status(self, submission_id: str, status: str, **metadata):
        """Publica transição do prompt quando o renderer oferece esse canal."""
        if not submission_id:
            return None
        normalized = str(status or "").strip().lower()
        with self._submission_lock:
            if (
                submission_id in self._cancelled_submission_ids
                and normalized not in {"cancelled", "completed", "failed"}
            ):
                return None
            if normalized in {"completed", "failed"}:
                self._active_submission_ids.discard(submission_id)
                self._cancelled_submission_ids.discard(submission_id)
            elif normalized == "cancelled":
                self._active_submission_ids.discard(submission_id)
                self._cancelled_submission_ids.add(submission_id)
            else:
                self._active_submission_ids.add(submission_id)
        if getattr(
            self._renderer, "supports_submission_status", False
        ) is not True:
            return None
        return self._renderer.update_submission_status(submission_id, status, **metadata)

    def register_submission(self, submission_id: str) -> None:
        """Registra aceitação antes de o loop de chat materializar a fila."""
        if not submission_id:
            return
        with self._submission_lock:
            self._active_submission_ids.add(submission_id)

    def _is_submission_cancelled(self, submission_id: str) -> bool:
        with self._submission_lock:
            return bool(
                submission_id and submission_id in self._cancelled_submission_ids
            )

    def _is_processing_cancelled(self, submission_id: str) -> bool:
        """Consulta o cancelamento estável da submission e depois o client global."""
        if self._is_submission_cancelled(submission_id):
            return True
        agent_client = self._agent_client
        is_cancelled = getattr(agent_client, "is_cancelled", None)
        return bool(callable(is_cancelled) and is_cancelled())

    def process_message(self, user, *, submission_id: str = ""):
        """Executa process chat message com controle de turno."""
        if self._is_submission_cancelled(submission_id):
            self.update_submission_status(submission_id, "cancelled")
            return
        agent_client = self._agent_client
        if agent_client is not None:
            agent_client.reset_cancel_state()
        if submission_id:
            with self._submission_lock:
                self._running_submission_ids.add(submission_id)
            self.update_submission_status(submission_id, "running")
        self._processing_tls.submission_id = submission_id
        try:
            self._do_process_message(user)
        except KeyboardInterrupt:
            self.update_submission_status(submission_id, "cancelled")
            raise
        except Exception as error:
            self.update_submission_status(
                submission_id,
                "failed",
                message=self._submission_error_message(error),
            )
            raise
        else:
            cancelled = self._is_processing_cancelled(submission_id)
            self.update_submission_status(
                submission_id,
                "cancelled" if cancelled else "completed",
            )
        finally:
            self._processing_tls.submission_id = ""
            if submission_id:
                with self._submission_lock:
                    self._running_submission_ids.discard(submission_id)
            if (
                self._turn_manager is not None
                and self._turn_manager.is_ai_turn
                and self._runtime_state.get_chat_outstanding_count() <= 1
            ):
                self._turn_manager.next_turn()

    def _do_process_message(self, user):
        """Executa uma rodada de chat com o contexto completo."""
        submission_id = str(getattr(self._processing_tls, "submission_id", "") or "")
        ctx = ChatRoundContext(
            session_services=self._session_services,
            task_services=self._task_services,
            renderer=self._renderer,
            session_state=self._session_state,
            parse_routing=self._parse_routing,
            parse_response=self._parse_response,
            dispatch_services=self._dispatch_services,
            show_system_message=self._system_layer.show_system_message,
            ui_queue=self._ui_event_queue,
            is_cancelled=lambda sid=submission_id: self._is_processing_cancelled(sid),
        )
        self._chat_round_orchestrator.process(user, ctx=ctx)

    def handle_local_interrupt(self) -> None:
        """Cancela só o processamento atual e devolve o chat ao input."""
        agent_client = self._agent_client
        if agent_client is not None:
            agent_client.cancel_active_work()
            agent_client._show_cancelled_once()
        with self._submission_lock:
            active_submission_ids = list(self._active_submission_ids)
            futures = list(self._submission_futures.values())
        for submission_id in active_submission_ids:
            self.update_submission_status(submission_id, "cancelled")
        for future, _submission_id, _slot_reserved in futures:
            future.cancel()
        if self._renderer is not None:
            self._renderer.reset_visual_state()
        if self._turn_manager is not None:
            self._turn_manager.reset()
        self._refresh_parallel_toolbar()

    def process_async_message(self, user, *, submission_id: str = ""):
        """Processa um prompt vindo da fila assíncrona e libera o slot ao final."""
        try:
            self.process_message(user, submission_id=submission_id)
        finally:
            remaining = self._runtime_state.decrement_chat_inflight(self._refresh_parallel_toolbar)
            self._runtime_state.release_chat_slot()
            if (
                remaining == 0
                and self._runtime_state.get_chat_pending_count() == 0
                and self._turn_manager is not None
                and self._turn_manager.is_ai_turn
            ):
                self._turn_manager.next_turn()

    def process_queued_message(self, user, *, submission_id: str = ""):
        """Promove um prompt pendente quando um worker do executor fica livre."""
        slot_semaphore = getattr(self._runtime_state, "chat_slot_semaphore", None)
        slot_acquired = slot_semaphore is None
        promoted = False
        try:
            if slot_semaphore is not None:
                deadline = time.monotonic() + self._queue_wait_timeout_seconds
                while not slot_acquired:
                    if self._is_submission_cancelled(submission_id):
                        self.update_submission_status(submission_id, "cancelled")
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ChatQueueWaitTimeoutError(
                            "chat slot semaphore did not become available"
                        )
                    slot_acquired = bool(
                        slot_semaphore.acquire(timeout=min(0.25, remaining))
                    )
            if not slot_acquired:
                return
            self._runtime_state.promote_chat_pending_to_inflight(
                self._refresh_parallel_toolbar
            )
            promoted = True
            self.update_submission_status(submission_id, "starting")
            self.process_message(user, submission_id=submission_id)
        finally:
            if promoted:
                remaining = self._runtime_state.decrement_chat_inflight(
                    self._refresh_parallel_toolbar
                )
                self._runtime_state.release_chat_slot()
                if (
                    remaining == 0
                    and self._runtime_state.get_chat_pending_count() == 0
                    and self._turn_manager is not None
                    and self._turn_manager.is_ai_turn
                ):
                    self._turn_manager.next_turn()
            else:
                self._runtime_state.decrement_chat_pending(
                    self._refresh_parallel_toolbar
                )
                if slot_acquired:
                    self._runtime_state.release_chat_slot()

    def submit_async_message(self, user, *, slot_reserved=True, submission_id: str = ""):
        """Submete um prompt já reservado para a pool de execução do chat."""
        chat_executor = getattr(self._runtime_state, "chat_executor", None)
        if chat_executor is None:
            raise RuntimeError("chat executor não inicializado")
        if self._is_submission_cancelled(submission_id):
            self._release_unstarted_slot(slot_reserved)
            with self._submission_lock:
                self._cancelled_submission_ids.discard(submission_id)
            return None
        try:
            target = self.process_async_message if slot_reserved else self.process_queued_message
            if slot_reserved:
                self.update_submission_status(submission_id, "starting")
            submit_kwargs = {"submission_id": submission_id} if submission_id else {}
            future = chat_executor.submit(target, user, **submit_kwargs)
            with self._submission_lock:
                self._submission_futures[id(future)] = (
                    future,
                    submission_id,
                    slot_reserved,
                )
            future.add_done_callback(
                lambda completed, sid=submission_id, reserved=slot_reserved: (
                    self._on_submission_done(completed, sid, reserved)
                )
            )
            self._refresh_parallel_toolbar()
            return future
        except Exception as error:
            self.update_submission_status(
                submission_id,
                "failed",
                message=self._submission_error_message(error),
            )
            if slot_reserved:
                self._runtime_state.decrement_chat_inflight(self._refresh_parallel_toolbar)
                self._runtime_state.release_chat_slot()
            else:
                self._runtime_state.decrement_chat_pending(self._refresh_parallel_toolbar)
            self._refresh_parallel_toolbar()
            raise

    def process_sync_message_with_slot(self, user, *, submission_id: str = ""):
        """Executa um prompt no thread principal ocupando um slot de concorrência."""
        slot_semaphore = getattr(self._runtime_state, "chat_slot_semaphore", None)
        if slot_semaphore is not None:
            slot_semaphore.acquire()
        self._runtime_state.increment_chat_inflight(self._refresh_parallel_toolbar)
        try:
            self.update_submission_status(submission_id, "starting")
            self.process_message(user, submission_id=submission_id)
        finally:
            self._runtime_state.decrement_chat_inflight(self._refresh_parallel_toolbar)
            self._runtime_state.release_chat_slot()

    def drain_ui_events(self, ui_queue) -> None:
        """Consome todos os RenderEvents pendentes na fila e chama renderer na main thread."""
        self._ui_event_handler.drain_ui_events(ui_queue)

    def _on_submission_done(
        self,
        future,
        submission_id: str,
        slot_reserved: bool,
    ) -> None:
        """Observa Future para impedir falhas anteriores ao lifecycle visual."""
        with self._submission_lock:
            self._submission_futures.pop(id(future), None)
        if future.cancelled():
            self.update_submission_status(submission_id, "cancelled")
            self._release_unstarted_slot(slot_reserved)
            with self._submission_lock:
                self._cancelled_submission_ids.discard(submission_id)
            return
        try:
            error = future.exception()
        except Exception as callback_error:
            error = callback_error
        if error is None:
            with self._submission_lock:
                self._cancelled_submission_ids.discard(submission_id)
            return
        message = self._submission_error_message(error)
        logger.error(
            "falha assíncrona no chat submission_id=%s: %s",
            submission_id or "sem-id",
            message,
        )
        self.update_submission_status(submission_id, "failed", message=message)
        if getattr(self._renderer, "supports_submission_status", False) is not True:
            self._system_layer.show_error_message(
                f"[erro] falha ao iniciar execução do chat: {message}"
            )

    def _release_unstarted_slot(self, slot_reserved: bool) -> None:
        """Libera contadores quando uma Future é cancelada antes de executar."""
        if slot_reserved:
            self._runtime_state.decrement_chat_inflight(
                self._refresh_parallel_toolbar
            )
            self._runtime_state.release_chat_slot()
        else:
            self._runtime_state.decrement_chat_pending(
                self._refresh_parallel_toolbar
            )
        self._refresh_parallel_toolbar()

    @staticmethod
    def _submission_error_message(error: BaseException) -> str:
        message = str(getattr(error, "user_message", "") or "").strip()
        if not message:
            message = str(error or "").strip() or type(error).__name__
        return message if len(message) <= 240 else f"{message[:237]}..."
