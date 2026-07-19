"""Ciclo de vida do processamento de chat."""

from __future__ import annotations

from .chat_round import ChatRoundContext


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

    def bind_ui_event_queue(self, ui_event_queue) -> None:
        """Vincula a fila de eventos de UI materializada pelo loop de chat."""
        self._ui_event_queue = ui_event_queue

    def process_message(self, user):
        """Executa process chat message com controle de turno."""
        agent_client = self._agent_client
        if agent_client is not None:
            agent_client.reset_cancel_state()
        try:
            self._do_process_message(user)
        finally:
            if (
                self._turn_manager is not None
                and self._turn_manager.is_ai_turn
                and self._runtime_state.get_chat_outstanding_count() <= 1
            ):
                self._turn_manager.next_turn()

    def _do_process_message(self, user):
        """Executa uma rodada de chat com o contexto completo."""
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
        )
        self._chat_round_orchestrator.process(user, ctx=ctx)

    def handle_local_interrupt(self) -> None:
        """Cancela só o processamento atual e devolve o chat ao input."""
        agent_client = self._agent_client
        if agent_client is not None:
            agent_client.cancel_active_work()
            agent_client._show_cancelled_once()
        if self._renderer is not None:
            self._renderer.reset_visual_state()
        if self._turn_manager is not None:
            self._turn_manager.reset()
        self._refresh_parallel_toolbar()

    def process_async_message(self, user):
        """Processa um prompt vindo da fila assíncrona e libera o slot ao final."""
        try:
            self.process_message(user)
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

    def process_queued_message(self, user):
        """Promove um prompt pendente quando um worker do executor fica livre."""
        slot_semaphore = getattr(self._runtime_state, "chat_slot_semaphore", None)
        if slot_semaphore is not None:
            slot_semaphore.acquire()
        promoted = False
        try:
            self._runtime_state.promote_chat_pending_to_inflight(
                self._refresh_parallel_toolbar
            )
            promoted = True
            self.process_message(user)
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
                self._runtime_state.release_chat_slot()

    def submit_async_message(self, user, *, slot_reserved=True):
        """Submete um prompt já reservado para a pool de execução do chat."""
        chat_executor = getattr(self._runtime_state, "chat_executor", None)
        if chat_executor is None:
            raise RuntimeError("chat executor não inicializado")
        try:
            target = self.process_async_message if slot_reserved else self.process_queued_message
            chat_executor.submit(target, user)
            self._refresh_parallel_toolbar()
        except Exception:
            if slot_reserved:
                self._runtime_state.decrement_chat_inflight(self._refresh_parallel_toolbar)
                self._runtime_state.release_chat_slot()
            else:
                self._runtime_state.decrement_chat_pending(self._refresh_parallel_toolbar)
            self._refresh_parallel_toolbar()
            raise

    def process_sync_message_with_slot(self, user):
        """Executa um prompt no thread principal ocupando um slot de concorrência."""
        slot_semaphore = getattr(self._runtime_state, "chat_slot_semaphore", None)
        if slot_semaphore is not None:
            slot_semaphore.acquire()
        self._runtime_state.increment_chat_inflight(self._refresh_parallel_toolbar)
        try:
            self.process_message(user)
        finally:
            self._runtime_state.decrement_chat_inflight(self._refresh_parallel_toolbar)
            self._runtime_state.release_chat_slot()

    def drain_ui_events(self, ui_queue) -> None:
        """Consome todos os RenderEvents pendentes na fila e chama renderer na main thread."""
        self._ui_event_handler.drain_ui_events(ui_queue)
