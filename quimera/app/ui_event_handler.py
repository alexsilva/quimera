"""Event handler de UI — responsável por inscrever eventos de domínio e drenar a fila de render."""
from __future__ import annotations

import sys
import threading
import queue
from contextlib import nullcontext

from .render_event import RenderEvent
from ..tasks.events import (
    BugFiled,
    TaskCompleted,
    TaskFailed,
    TaskProposed,
    TaskRequeued,
    TaskSubmittedForReview,
)
from ..tasks.utils import summarize_task_feedback
from .config import logger
from ..ui.textual.constants import (
    format_failover_message,
    format_retry_message,
)


class UiEventHandler:
    """Inscreve handlers em eventos de domínio (EventSink) e drena a fila de UI na main thread."""

    def __init__(
        self,
        *,
        renderer,
        input_gate,
        runtime_state,
        system_layer,
        event_sink,
        show_muted_message,
        show_system_message,
        show_warning_message,
        show_error_message,
        redisplay_user_prompt,
        output_lock,
    ):
        self._renderer = renderer
        self._input_gate = input_gate
        self._runtime_state = runtime_state
        self._system_layer = system_layer
        self._event_sink = event_sink
        self._show_muted_message = show_muted_message
        self._show_system_message = show_system_message
        self._show_warning_message = show_warning_message
        self._show_error_message = show_error_message
        self._redisplay_user_prompt = redisplay_user_prompt
        self._output_lock = output_lock
        self._subscriptions: list = []

    # ------------------------------------------------------------------
    # Inscrição em eventos de domínio
    # ------------------------------------------------------------------

    def wire_event_ui(self) -> list:
        """Conecta eventos de domínio aos callbacks de UI. Retorna lista de subscriptions."""
        def _on_task_completed(event):
            line = f"[task {event.task_id}] concluída"
            if event.reviewed_by:
                line = f"{line} | aprovada por {event.reviewed_by}"
            summary = summarize_task_feedback(event.result)
            if summary:
                line = f"{line}: {summary}"
            self._show_muted_message(line)

        def _on_task_failed(event):
            sl = self._system_layer
            if sl is not None and hasattr(sl, "show_warning_message"):
                sl.show_warning_message(f"[task {event.task_id}] falhou: {event.reason or 'sem motivo'}")
            else:
                self._renderer.show_warning(f"[task {event.task_id}] falhou: {event.reason or 'sem motivo'}")

        def _on_task_proposed(event):
            self._show_system_message(f"[task {event.task_id}] proposta: {event.description[:60]}")

        def _on_task_submitted(event):
            self._show_muted_message(f"[task {event.task_id}] submetida para revisão")

        def _on_task_requeued(event):
            sl = self._system_layer
            if sl is not None and hasattr(sl, "show_warning_message"):
                sl.show_warning_message(f"[task {event.task_id}] requeue (tentativa {event.attempt})")
            else:
                self._renderer.show_warning(f"[task {event.task_id}] requeue (tentativa {event.attempt})")

        def _on_bug_filed(event):
            severity_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                event.severity.lower(), "⚪"
            )
            self._show_muted_message(
                f"{severity_icon} [bug {event.bug_id}] {event.category}: {event.summary}"
            )

        self._subscriptions = [
            self._event_sink.subscribe(TaskCompleted, _on_task_completed),
            self._event_sink.subscribe(TaskFailed, _on_task_failed),
            self._event_sink.subscribe(TaskProposed, _on_task_proposed),
            self._event_sink.subscribe(TaskSubmittedForReview, _on_task_submitted),
            self._event_sink.subscribe(TaskRequeued, _on_task_requeued),
            self._event_sink.subscribe(BugFiled, _on_bug_filed),
        ]
        return self._subscriptions

    # ------------------------------------------------------------------
    # Auxiliares de renderização acima do prompt
    # ------------------------------------------------------------------

    def _render_agent_activity(self, agent, activity: str, meta: dict) -> None:
        """Renderiza atividade estruturada de agente com fallback textual.

        Renderers com canal estruturado (Textual) recebem os campos separados;
        os legados caem numa frase pt-BR de sistema/aviso equivalente.
        """
        structured = self._renderer.supports_structured_agent_activity
        if activity == "failover":
            target = str(meta.get("target") or "").strip()
            message = str(meta.get("message") or "não respondeu").strip()
            if structured:
                self._renderer.notify_agent_failover(agent, target=target, message=message)
                return
            self._show_system_message(
                format_failover_message(str(agent or ""), target, message)
            )
            return
        if activity == "retrying":
            reason = str(meta.get("reason") or "")
            attempt = int(meta.get("attempt") or 0)
            limit = int(meta.get("limit") or 0)
            detail = str(meta.get("detail") or "")
            if structured:
                self._renderer.notify_agent_retry(
                    agent, reason=reason, attempt=attempt, limit=limit, detail=detail
                )
                return
            self._show_warning_message(
                format_retry_message(reason, attempt, limit, detail)
            )

    def _should_render_ui_event_above_prompt(self) -> bool:
        """Retorna True quando há prompt ativo controlado por outra thread."""
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return False
        input_gate = self._input_gate
        if input_gate is not None:
            is_active = getattr(input_gate, "is_active", None)
            get_owner_thread_id = getattr(input_gate, "get_owner_thread_id", None)
            if callable(is_active) and callable(get_owner_thread_id):
                try:
                    active_state = is_active()
                    if not isinstance(active_state, bool) or not active_state:
                        return False
                    owner_thread_id = get_owner_thread_id()
                except Exception:
                    owner_thread_id = None
                if not isinstance(owner_thread_id, int):
                    return False
                return owner_thread_id != threading.get_ident()
        status_lock = getattr(self._runtime_state, "nonblocking_input_status_lock", nullcontext())
        with status_lock:
            if self._runtime_state.nonblocking_input_status != "reading":
                return False
        owner_thread_id = getattr(self._runtime_state, "prompt_owning_thread_id", None)
        if owner_thread_id is None:
            return False
        return owner_thread_id != threading.get_ident()

    def _run_ui_event_above_prompt(self, callback) -> bool:
        """Tenta renderizar callback acima do prompt ativo via InputGate."""
        if not callable(callback):
            return False
        run_in_terminal_message = getattr(self._input_gate, "run_in_terminal_message", None)
        if not callable(run_in_terminal_message):
            return False
        output_lock = self._output_lock

        def _render_callback() -> None:
            with output_lock:
                callback()
                self._renderer.flush()

        try:
            return bool(run_in_terminal_message(_render_callback))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Drenagem da fila de UI
    # ------------------------------------------------------------------

    def drain_ui_events(self, ui_queue: "queue.Queue") -> None:
        """Consome todos os RenderEvents pendentes na fila e chama renderer na main thread."""
        while True:
            try:
                event: RenderEvent = ui_queue.get_nowait()
            except Exception:
                break
            try:
                event_type = event.type
                if event_type == RenderEvent.SYSTEM:
                    self._show_muted_message(event.payload)
                elif event_type == RenderEvent.TEXT:
                    no_response = (event.metadata or {}).get("no_response", False)
                    agent_name = str(event.agent or "").strip()

                    if not agent_name:
                        payload = str(event.payload or "").strip()
                        if payload:
                            self._show_muted_message(payload)
                        continue

                    payload = event.payload

                    def _render_text_event(
                        _no_response=no_response,
                        _agent_name=agent_name,
                        _payload=payload,
                    ) -> None:
                        if _no_response:
                            self._renderer.show_no_response(_agent_name)
                        else:
                            self._renderer.show_message(_agent_name, _payload)

                    if self._should_render_ui_event_above_prompt():
                        if not self._run_ui_event_above_prompt(_render_text_event):
                            _render_text_event()
                            self._redisplay_user_prompt(clear_first=False)
                        continue
                    _render_text_event()
                elif event_type == RenderEvent.WARNING:
                    self._show_warning_message(event.payload)
                elif event_type == RenderEvent.ERROR:
                    self._show_error_message(event.payload)
                elif event_type == RenderEvent.DELEGATION:
                    meta = event.metadata or {}
                    delegation_agent = event.agent
                    delegation_to = meta.get("to")
                    delegation_task = meta.get("task")
                    delegation_id = meta.get("delegation_id")
                    delegation_chain = meta.get("chain")

                    def _render_delegation_event(
                        _agent=delegation_agent,
                        _to=delegation_to,
                        _task=delegation_task,
                        _delegation_id=delegation_id,
                        _chain=delegation_chain,
                    ) -> None:
                        self._renderer.show_delegation(
                            _agent,
                            _to,
                            task=_task,
                            delegation_id=_delegation_id,
                            chain=_chain,
                        )

                    if self._should_render_ui_event_above_prompt():
                        if not self._run_ui_event_above_prompt(_render_delegation_event):
                            _render_delegation_event()
                            self._redisplay_user_prompt(clear_first=False)
                        continue
                    _render_delegation_event()
                elif event_type == RenderEvent.AGENT_ACTIVITY:
                    meta = event.metadata or {}
                    activity = str(meta.get("activity") or "").strip().lower()
                    activity_agent = event.agent

                    def _render_agent_activity_event(
                        _agent=activity_agent,
                        _activity=activity,
                        _meta=meta,
                    ) -> None:
                        self._render_agent_activity(_agent, _activity, _meta)

                    if self._should_render_ui_event_above_prompt():
                        if not self._run_ui_event_above_prompt(_render_agent_activity_event):
                            _render_agent_activity_event()
                            self._redisplay_user_prompt(clear_first=False)
                        continue
                    _render_agent_activity_event()
                elif event_type == RenderEvent.TURN_SUMMARY:
                    summary_agent = event.agent
                    summary_payload = event.payload

                    def _render_turn_summary_event(
                        _agent=summary_agent,
                        _payload=summary_payload,
                    ) -> None:
                        self._renderer.show_turn_summary(_agent, _payload)

                    if self._should_render_ui_event_above_prompt():
                        if not self._run_ui_event_above_prompt(_render_turn_summary_event):
                            _render_turn_summary_event()
                            self._redisplay_user_prompt(clear_first=False)
                        continue
                    _render_turn_summary_event()
                elif event_type == RenderEvent.REDISPLAY:
                    self._renderer.flush()
                    self._redisplay_user_prompt(clear_first=False)
                elif event_type == RenderEvent.EVENT:
                    meta = event.metadata or {}
                    event_obj = meta.get("event_obj")
                    if event_obj is not None and hasattr(self._event_sink, "_dispatch"):
                        self._event_sink._dispatch(event_obj)
            except Exception:
                logger.exception("drain_ui_events: erro ao processar evento type=%s", event.type)
            finally:
                try:
                    ui_queue.task_done()
                except Exception:
                    pass
