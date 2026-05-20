"""AgentGateway — chamada bruta a agentes: prompt, backend, streaming."""
import queue as _queue_module
import time
from contextlib import nullcontext

from ..prompt_kinds import PromptKind
from .config import logger
from .render_event import RenderEvent


def _is_user_cancelled(agent_client) -> bool:
    """Retorna True quando há sinal explícito de cancelamento do usuário."""
    if agent_client is None:
        return False
    if getattr(agent_client, "_user_cancelled", False) is True:
        return True
    cancel_event = getattr(agent_client, "_cancel_event", None)
    is_set = getattr(cancel_event, "is_set", None)
    if callable(is_set):
        try:
            return is_set() is True
        except Exception:
            return False
    return False


class AgentGateway:
    """Executa chamada bruta: monta prompt, chama backend, gerencia streaming e registra métricas básicas.

    Dependências injetadas explicitamente — sem acesso direto a QuimeraApp.
    """

    def __init__(
        self,
        agent_client,
        prompt_builder,
        renderer,
        plugin_resolver,
        get_history,
        get_shared_state,
        get_execution_mode,
        refresh_task_state,
        session_state,
        increment_call_index,
        get_round_index,
        debug_prompt_metrics=False,
        clear_prompt_line=None,
        redisplay_prompt=None,
        update_session=None,
        output_lock=None,
        counter_lock=None,
        ui_queue: "_queue_module.Queue | None" = None,
    ):
        self._agent_client = agent_client
        self._prompt_builder = prompt_builder
        self._renderer = renderer
        self._plugin_resolver = plugin_resolver
        self._get_history = get_history
        self._get_shared_state = get_shared_state
        self._get_execution_mode = get_execution_mode
        self._refresh_task_state = refresh_task_state
        self._session_state = session_state
        self._increment_call_index = increment_call_index
        self._get_round_index = get_round_index
        self._debug_prompt_metrics = debug_prompt_metrics
        self._clear_prompt_line = clear_prompt_line or (lambda: None)
        self._redisplay_prompt = redisplay_prompt or (lambda **kw: None)
        self._update_session = update_session or (lambda *a: None)
        self._output_lock = output_lock
        self._counter_lock = counter_lock
        self._ui_queue = ui_queue

    def call(
        self,
        agent,
        is_first_speaker=False,
        handoff=None,
        primary=True,
        protocol_mode="standard",
        handoff_only=False,
        silent=False,
        show_output=True,
        from_agent=None,
        prompt_kind=PromptKind.CHAT,
        history_snapshot=None,
        request_override=None,
    ):
        """Monta o prompt final e executa a chamada ao backend do agente."""
        agent_client = self._agent_client
        if _is_user_cancelled(agent_client):
            logger.debug("[GATEWAY] agent=%s cancelled by user before low-level call, aborting", agent)
            return None

        call_index_snapshot = self._increment_call_index()
        start = time.time()
        if history_snapshot is None:
            history = self._get_history()
        else:
            history = history_snapshot
        if history is None:
            history = []
        elif not isinstance(history, list):
            history = list(history)
        self._refresh_task_state()

        output_lock = self._output_lock

        _stream_buffer = []
        def _on_text_chunk(chunk):
            if silent or not show_output or not chunk:
                return
            _stream_buffer.append(chunk)

        shared_state = self._get_shared_state()
        active_execution_mode = self._get_execution_mode()

        if self._debug_prompt_metrics:
            prompt, metrics = self._prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                handoff,
                debug=True,
                primary=primary,
                shared_state=shared_state,
                handoff_only=handoff_only,
                from_agent=from_agent,
                skip_tool_prompt=True,
                execution_mode=active_execution_mode,
                prompt_kind=prompt_kind,
                request_override=request_override,
            )
            agent_client.log_prompt_metrics(
                agent,
                metrics,
                session_id=self._session_state.get("session_id") if self._session_state else None,
                round_index=self._get_round_index(),
                session_call_index=call_index_snapshot,
                history_window=self._prompt_builder.history_window,
                protocol_mode=protocol_mode,
            )
        else:
            prompt = self._prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                handoff,
                primary=primary,
                shared_state=shared_state,
                handoff_only=handoff_only,
                from_agent=from_agent,
                skip_tool_prompt=True,
                execution_mode=active_execution_mode,
                prompt_kind=prompt_kind,
                request_override=request_override,
            )

        if _is_user_cancelled(agent_client):
            logger.debug("[GATEWAY] agent=%s cancelled by user before backend call, aborting", agent)
            return None

        result = agent_client.call(agent, prompt, silent=silent, on_text_chunk=_on_text_chunk)

        if _stream_buffer or result:
            if self._ui_queue is not None:
                self._ui_queue.put(RenderEvent(RenderEvent.REDISPLAY, "", agent=agent))
            else:
                renderer = self._renderer
                with (output_lock if output_lock is not None else nullcontext()):
                    self._clear_prompt_line()
                    flush = getattr(renderer, "flush", None)
                    if callable(flush):
                        flush()
                    self._redisplay_prompt(clear_first=False)

        agent_client.flush_pending_summary()
        elapsed = time.time() - start
        self._update_session(agent, bool(result), elapsed)
        logger.debug("[GATEWAY] agent=%s latency=%.2fs result=%s", agent, elapsed, "ok" if result else "none")
        return result
