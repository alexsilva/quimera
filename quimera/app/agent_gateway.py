"""AgentGateway — chamada bruta a agentes: prompt, backend, streaming."""
import queue as _queue_module
import re
import time
from contextlib import nullcontext

from ..prompt_kinds import PromptKind
from .agent_run_events import AgentRunEvent, coerce_agent_run_sink
from .config import logger
from .render_event import RenderEvent


class _ThinkingStreamRelay:
    """Detecta blocos <think>/<thinking> em um stream e os exibe ao vivo no feed do agente.

    Espelha o comportamento de agentes CLI, cujo stdout bruto (incluindo o
    raciocínio do modelo) já aparece no feed transitório enquanto o turno roda.
    """

    _OPEN_RE = re.compile(r"<think(?:ing)?>")
    _CLOSE_RE = re.compile(r"</think(?:ing)?>")
    _TAIL_KEEP = 12

    def __init__(self, renderer, agent) -> None:
        self._renderer = renderer
        self._agent = agent
        self._buffer = ""
        self._in_think = False
        self._thinking_text = ""

    def feed(self, chunk_text: str) -> None:
        """Processa um novo pedaço de texto bruto do stream."""
        if not chunk_text:
            return
        self._buffer += chunk_text
        while True:
            if not self._in_think:
                match = self._OPEN_RE.search(self._buffer)
                if not match:
                    self._buffer = self._buffer[-self._TAIL_KEEP:]
                    return
                self._in_think = True
                self._buffer = self._buffer[match.end():]
                self._thinking_text = ""
                continue
            match = self._CLOSE_RE.search(self._buffer)
            if not match:
                self._thinking_text += self._buffer[:-self._TAIL_KEEP] if len(self._buffer) > self._TAIL_KEEP else ""
                self._buffer = self._buffer[-self._TAIL_KEEP:]
                self._publish()
                return
            self._thinking_text += self._buffer[:match.start()]
            self._buffer = self._buffer[match.end():]
            self._in_think = False
            self._publish()
            self._thinking_text = ""

    def _publish(self) -> None:
        text = self._thinking_text.strip()
        if not text:
            return
        update = getattr(self._renderer, "update_agent_transient", None)
        if callable(update):
            update(self._agent, f"[thinking] {text}")


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
        profile_resolver,
        get_history,
        get_shared_state,
        get_execution_mode,
        refresh_task_state,
        session_state,
        increment_call_index,
        get_round_index,
        debug_prompt_metrics=False,
        redisplay_prompt=None,
        update_session=None,
        output_lock=None,
        counter_lock=None,
        ui_queue: "_queue_module.Queue | None" = None,
        agent_run_sink=None,
    ):
        self._agent_client = agent_client
        self._prompt_builder = prompt_builder
        self._renderer = renderer
        self._profile_resolver = profile_resolver
        self._get_history = get_history
        self._get_shared_state = get_shared_state
        self._get_execution_mode = get_execution_mode
        self._refresh_task_state = refresh_task_state
        self._session_state = session_state
        self._increment_call_index = increment_call_index
        self._get_round_index = get_round_index
        self._debug_prompt_metrics = debug_prompt_metrics
        self._redisplay_prompt = redisplay_prompt or (lambda **kw: None)
        self._update_session = update_session or (lambda *a: None)
        self._output_lock = output_lock
        self._counter_lock = counter_lock
        self._ui_queue = ui_queue
        self._agent_run_sink = coerce_agent_run_sink(agent_run_sink)

    def call(
        self,
        agent,
        is_first_speaker=False,
        delegation=None,
        primary=True,
        protocol_mode="standard",
        delegation_only=False,
        silent=False,
        show_output=True,
        from_agent=None,
        prompt_kind=PromptKind.CHAT,
        history_snapshot=None,
        request_override=None,
        progress_callback=None,
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
        event_metadata = {
            "prompt_kind": getattr(prompt_kind, "value", str(prompt_kind)),
            "protocol_mode": protocol_mode,
            "delegation_only": bool(delegation_only),
            "primary": bool(primary),
            "silent": bool(silent),
            "show_output": bool(show_output),
            "from_agent": from_agent,
        }
        self._agent_run_sink.emit(
            AgentRunEvent("started", str(agent), metadata=event_metadata)
        )

        _stream_buffer = []
        thinking_relay = (
            _ThinkingStreamRelay(self._renderer, agent)
            if not silent and show_output and self._renderer is not None
            else None
        )

        def _on_text_chunk(chunk):
            if chunk:
                self._agent_run_sink.emit(
                    AgentRunEvent("delta", str(agent), text=str(chunk), metadata=event_metadata)
                )
            if silent or not show_output or not chunk:
                return
            if thinking_relay is not None:
                text = chunk.get("text") if isinstance(chunk, dict) else chunk
                thinking_relay.feed(text)
            _stream_buffer.append(chunk)

        shared_state = self._get_shared_state()
        active_execution_mode = self._get_execution_mode()

        if self._debug_prompt_metrics:
            prompt, metrics = self._prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                delegation,
                debug=True,
                primary=primary,
                shared_state=shared_state,
                delegation_only=delegation_only,
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
                delegation,
                primary=primary,
                shared_state=shared_state,
                delegation_only=delegation_only,
                from_agent=from_agent,
                skip_tool_prompt=True,
                execution_mode=active_execution_mode,
                prompt_kind=prompt_kind,
                request_override=request_override,
            )

        if _is_user_cancelled(agent_client):
            logger.debug("[GATEWAY] agent=%s cancelled by user before backend call, aborting", agent)
            return None

        try:
            result = agent_client.call(
                agent,
                prompt,
                silent=silent,
                on_text_chunk=_on_text_chunk,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            fail_metadata = dict(event_metadata)
            fail_metadata["error"] = str(exc)
            self._agent_run_sink.emit(
                AgentRunEvent("failed", str(agent), metadata=fail_metadata)
            )
            raise

        if _stream_buffer or result:
            if self._ui_queue is not None:
                self._ui_queue.put(RenderEvent(RenderEvent.REDISPLAY, "", agent=agent))
            else:
                renderer = self._renderer
                with (output_lock if output_lock is not None else nullcontext()):
                    flush = getattr(renderer, "flush", None)
                    if callable(flush):
                        flush()
                    self._redisplay_prompt(clear_first=False)

        agent_client.flush_pending_summary()
        elapsed = time.time() - start
        finish_metadata = dict(event_metadata)
        finish_metadata["elapsed"] = elapsed
        self._agent_run_sink.emit(
            AgentRunEvent(
                "finished" if result else "failed",
                str(agent),
                text=str(result or ""),
                metadata=finish_metadata,
            )
        )
        self._update_session(agent, bool(result), elapsed)
        logger.debug("[GATEWAY] agent=%s latency=%.2fs result=%s", agent, elapsed, "ok" if result else "none")
        return result
