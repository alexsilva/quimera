"""Componentes de `quimera.app.dispatch`."""
import queue as _queue_module
import time
from contextlib import nullcontext

from ..prompt_kinds import PromptKind
from .agent_call_service import AgentCallService
from .agent_gateway import AgentGateway, _is_user_cancelled
from .render_event import RenderEvent
from .tool_loop import ToolLoopService
from .config import logger
from ..domain.session_state import SessionState


class AppDispatchServices:
    """Coordena AgentGateway e ToolLoopService; mantém spy telemetry."""

    _MAX_SPY_TOOLS = 12
    _MAX_SPY_TEXT_CHARS = 280
    _MAX_SPY_MAP_ITEMS = 6

    def __init__(
        self,
        *,
        agent_client_override=None,
        tool_executor_override=None,
        cancel_checker_override=None,
        ui_queue: "_queue_module.Queue | None" = None,
        prompt_builder=None,
        renderer=None,
        get_agent_plugin=None,
        session_state: "SessionState | None" = None,
        get_execution_mode=None,
        refresh_task_state=None,
        debug_prompt_metrics=False,
        clear_prompt_line=None,
        redisplay_prompt=None,
        output_lock=None,
        counter_lock=None,
        session_metrics=None,
        print_response_fn=None,
        persist_message_fn=None,
        record_session_metric=None,
        record_tool_event_fn=None,
        max_retries=2,
        retry_backoff=1,
        rate_limit_backoff=1,
        record_failure=None,
        record_success=None,
        get_agent_client=None,
        get_tool_executor=None,
        get_call_agent_fn_override=None,
        # compat: aceita lambdas individuais quando session_state não é fornecido
        get_history=None,
        get_shared_state=None,
        get_session_state=None,
        get_round_index=None,
        get_session_call_index=None,
        set_session_call_index=None,
        get_shared_state_lock=None,
    ):
        self._agent_client_override = agent_client_override
        self._tool_executor_override = tool_executor_override
        self._cancel_checker_override = cancel_checker_override
        self._ui_queue = ui_queue
        self._prompt_builder = prompt_builder
        self._renderer = renderer
        self._get_agent_plugin = get_agent_plugin
        self._session_state = session_state
        # compat: lambdas individuais (usadas quando session_state é None)
        self._get_history = get_history
        self._get_shared_state = get_shared_state
        self._get_session_state = get_session_state
        self._get_round_index = get_round_index
        self._get_session_call_index = get_session_call_index
        self._set_session_call_index = set_session_call_index
        self._get_shared_state_lock = get_shared_state_lock
        self._get_execution_mode = get_execution_mode
        self._refresh_task_state = refresh_task_state
        self._debug_prompt_metrics = debug_prompt_metrics
        self._clear_prompt_line = clear_prompt_line
        self._redisplay_prompt = redisplay_prompt
        self._output_lock = output_lock
        self._counter_lock = counter_lock
        self._session_metrics = session_metrics
        self._print_response_fn = print_response_fn
        self._persist_message_fn = persist_message_fn
        self._record_session_metric = record_session_metric
        self._record_tool_event_fn = record_tool_event_fn
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._rate_limit_backoff = rate_limit_backoff
        self._record_failure = record_failure
        self._record_success = record_success
        self._get_agent_client_fn = get_agent_client
        self._get_tool_executor_fn = get_tool_executor
        self._get_call_agent_fn_override = get_call_agent_fn_override
        self._gateway = None
        self._tool_loop = None
        self._agent_call_service = None

    @classmethod
    def from_app(cls, app, **kwargs):
        """Constrói AppDispatchServices a partir de um objeto app-like (compatibilidade)."""
        return cls(
            prompt_builder=lambda: getattr(app, 'prompt_builder', None),
            renderer=lambda: getattr(app, 'renderer', None),
            get_agent_plugin=lambda agent_name: (
                getattr(app, 'get_agent_plugin', lambda n: None)(agent_name)
            ),
            get_history=lambda: getattr(app, 'history', []),
            get_shared_state=lambda: getattr(app, 'shared_state', {}),
            get_session_state=lambda: getattr(app, 'session_state', {}),
            get_execution_mode=lambda: getattr(app, 'execution_mode', None),
            refresh_task_state=lambda: getattr(
                getattr(app, 'task_services', None), 'refresh_task_shared_state', lambda: None
            )(),
            get_round_index=lambda: getattr(app, 'round_index', 0),
            debug_prompt_metrics=lambda: getattr(app, 'debug_prompt_metrics', False),
            clear_prompt_line=lambda: getattr(app, '_clear_user_prompt_line_if_needed', lambda: None)(),
            redisplay_prompt=lambda **kw: getattr(app, '_redisplay_user_prompt_if_needed', lambda **kw_: None)(**kw),
            output_lock=lambda: getattr(app, '_output_lock', None),
            counter_lock=lambda: getattr(app, '_counter_lock', None),
            get_session_call_index=lambda: getattr(app, 'session_call_index', 0),
            set_session_call_index=lambda v: setattr(app, 'session_call_index', v),
            session_metrics=lambda: getattr(app, 'session_metrics', None),
            print_response_fn=lambda agent, text: getattr(app, 'print_response', lambda a, t: None)(agent, text),
            persist_message_fn=lambda agent, text: getattr(
                getattr(app, 'session_services', None), 'persist_message', lambda a, t: None
            )(agent, text),
            record_session_metric=lambda agent, metric, elapsed: (
                getattr(getattr(app, 'session_metrics', None), 'record_agent_metric', lambda *a: None)(
                    app, agent, metric, elapsed
                )
            ),
            record_tool_event_fn=lambda agent, **kw: (
                getattr(getattr(app, 'session_metrics', None), 'record_tool_event', lambda *a, **kw_: None)(
                    app, agent, **kw
                )
            ),
            max_retries=lambda: getattr(app, 'MAX_RETRIES', 2),
            retry_backoff=lambda: getattr(app, 'RETRY_BACKOFF_SECONDS', 1),
            rate_limit_backoff=lambda: getattr(app, 'RATE_LIMIT_BACKOFF_SECONDS', 1),
            record_failure=getattr(app, 'record_failure', None),
            record_success=getattr(app, 'record_success', None),
            get_shared_state_lock=lambda: getattr(app, '_shared_state_lock', None),
            get_agent_client=lambda: getattr(app, 'agent_client', None),
            get_tool_executor=lambda: getattr(app, 'tool_executor', None),
            get_call_agent_fn_override=lambda: getattr(app, '_call_agent', None),
            **kwargs,
        )

    @staticmethod
    def _call(val, *args, **kwargs):
        return val(*args, **kwargs) if callable(val) else val

    def _get_agent_client(self):
        return self._agent_client_override or (self._call(self._get_agent_client_fn) if self._get_agent_client_fn else None)

    def _get_tool_executor(self):
        return self._tool_executor_override or (self._call(self._get_tool_executor_fn) if self._get_tool_executor_fn else None)

    # Accessors que preferem SessionState mas caem nos lambdas legados
    def _history(self):
        if self._session_state is not None:
            return self._session_state.history
        return self._get_history() if self._get_history else []

    def _shared_state(self):
        if self._session_state is not None:
            return self._session_state.shared_state
        return self._get_shared_state() if self._get_shared_state else {}

    def _session_meta(self):
        if self._session_state is not None:
            return self._session_state.session_meta
        return self._get_session_state() if self._get_session_state else {}

    def _round_index(self):
        if self._session_state is not None:
            return self._session_state.round_index
        return self._get_round_index() if self._get_round_index else 0

    def _shared_state_lock(self):
        if self._session_state is not None:
            return self._session_state.shared_state_lock
        return self._get_shared_state_lock() if self._get_shared_state_lock else None

    def _increment_call_index(self):
        if self._session_state is not None:
            return self._session_state.increment_call_index()
        if self._get_session_call_index and self._set_session_call_index:
            current = self._get_session_call_index() + 1
            self._set_session_call_index(current)
            return current
        return 0

    # -------------------------------------------------------------------------
    # Lazy builders
    # -------------------------------------------------------------------------

    def _get_gateway(self) -> AgentGateway:
        if self._gateway is None:
            self._gateway = self._build_gateway()
        return self._gateway

    def _get_tool_loop(self) -> ToolLoopService:
        if self._tool_loop is None:
            self._tool_loop = self._build_tool_loop()
        return self._tool_loop

    def _build_gateway(self) -> AgentGateway:
        prompt_builder = self._call(self._prompt_builder)
        renderer = self._call(self._renderer)
        plugin_resolver = self._get_agent_plugin
        refresh_task_state = self._refresh_task_state
        debug_prompt_metrics = self._call(self._debug_prompt_metrics)
        clear_prompt_line = self._clear_prompt_line
        redisplay_prompt = self._redisplay_prompt
        output_lock = self._call(self._output_lock)
        counter_lock = self._call(self._counter_lock)
        session_state = self._session_state

        def _update_session(agent: str, success: bool, elapsed: float):
            ss = self._session_meta()
            if not ss:
                return
            try:
                ss["handoffs_sent"] += 1
                ss["total_latency"] += elapsed
                if success:
                    ss["handoffs_succeeded"] += 1
                else:
                    ss["handoffs_failed"] += 1
            except KeyError:
                pass
            if self._record_session_metric:
                self._record_session_metric(agent, "succeeded" if success else "failed", elapsed)

        return AgentGateway(
            agent_client=self._get_agent_client(),
            prompt_builder=prompt_builder,
            renderer=renderer,
            plugin_resolver=plugin_resolver,
            session_state=session_state,
            get_history=self._history,
            get_shared_state=self._shared_state,
            get_execution_mode=self._get_execution_mode,
            refresh_task_state=refresh_task_state,
            increment_call_index=self._increment_call_index,
            get_round_index=self._round_index,
            debug_prompt_metrics=debug_prompt_metrics,
            clear_prompt_line=clear_prompt_line,
            redisplay_prompt=redisplay_prompt,
            update_session=_update_session,
            output_lock=output_lock,
            counter_lock=counter_lock,
            ui_queue=self._ui_queue,
        )

    def _build_tool_loop(self) -> ToolLoopService:
        ui_queue = self._ui_queue
        plugin_resolver = self._get_agent_plugin
        print_response_fn = self._print_response_fn
        persist_message_fn = self._persist_message_fn
        record_tool_event_fn = self._record_tool_event_fn

        def _cancel_checker() -> bool:
            if callable(self._cancel_checker_override):
                return bool(self._cancel_checker_override())
            return _is_user_cancelled(self._get_agent_client())

        def _call_agent_fn(agent, **kwargs):
            if self._get_call_agent_fn_override is not None:
                override = self._get_call_agent_fn_override()
                if override is not None:
                    return override(agent, **kwargs)
            return self.call_agent_low_level(agent, **kwargs)

        def _record_tool_event_wrapper(agent, **kwargs):
            if record_tool_event_fn:
                record_tool_event_fn(agent, **kwargs)

        def _reset_approve_all():
            tool_executor = self._get_tool_executor()
            if tool_executor is None:
                return
            approval_handler = getattr(tool_executor, "approval_handler", None)
            if approval_handler is not None and hasattr(approval_handler, "reset_approve_all_after_cycle"):
                approval_handler.reset_approve_all_after_cycle()

        def _progress_callback(agent, tool_name, hop, max_hops, elapsed, ok, is_invalid):
            status = "✓" if ok else "✗" if is_invalid else "○"
            msg = f"[tool hop {hop}/{max_hops}] {agent} {status} {tool_name}"
            if ui_queue is not None:
                ui_queue.put(RenderEvent(RenderEvent.SYSTEM, msg))
            else:
                renderer = self._call(self._renderer)
                if renderer is not None:
                    show_system_neutral = getattr(renderer, "show_system_neutral", None)
                    if callable(show_system_neutral):
                        show_system_neutral(msg)

        return ToolLoopService(
            tool_executor=self._get_tool_executor(),
            plugin_resolver=plugin_resolver,
            call_agent_fn=_call_agent_fn,
            print_response_fn=print_response_fn,
            persist_message_fn=persist_message_fn,
            cancel_checker=_cancel_checker,
            record_tool_event=_record_tool_event_wrapper,
            reset_approve_all=_reset_approve_all,
            progress_callback=_progress_callback,
        )

    # -------------------------------------------------------------------------
    # Agent call service (retry)
    # -------------------------------------------------------------------------

    def _get_agent_call_service(self) -> AgentCallService:
        if self._agent_call_service is None:
            self._agent_call_service = self._build_agent_call_service()
        return self._agent_call_service

    def _build_agent_call_service(self) -> AgentCallService:
        agent_client = self._get_agent_client()

        def _is_rate_limited():
            return bool(agent_client and getattr(agent_client, 'rate_limit_detected', False))

        return AgentCallService(
            max_retries=self._call(self._max_retries),
            retry_backoff=self._call(self._retry_backoff),
            rate_limit_backoff=self._call(self._rate_limit_backoff),
            record_failure=self._record_failure,
            record_success=self._record_success,
            is_rate_limited=_is_rate_limited,
        )

    # -------------------------------------------------------------------------
    # Spy telemetry (permanece em AppDispatchServices)
    # -------------------------------------------------------------------------

    @classmethod
    def _truncate_spy_text(cls, value):
        if not isinstance(value, str):
            return value
        if len(value) <= cls._MAX_SPY_TEXT_CHARS:
            return value
        return value[: cls._MAX_SPY_TEXT_CHARS - 3] + "..."

    @classmethod
    def _sanitize_spy_map(cls, payload, max_items=None):
        if not isinstance(payload, dict):
            return None
        item_limit = max_items if isinstance(max_items, int) and max_items > 0 else cls._MAX_SPY_MAP_ITEMS
        sanitized = {}
        for index, (key, value) in enumerate(payload.items()):
            if index >= item_limit:
                break
            normalized_key = cls._truncate_spy_text(str(key))
            if isinstance(value, str):
                sanitized[normalized_key] = cls._truncate_spy_text(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                sanitized[normalized_key] = value
            else:
                sanitized[normalized_key] = cls._truncate_spy_text(str(value))
        return sanitized or None

    @classmethod
    def _sanitize_spy_turn_detail(cls, detail):
        if not isinstance(detail, dict):
            return None

        tools = detail.get("tools")
        if not isinstance(tools, list):
            tools = []

        sanitized_tools = []
        for tool in tools[: cls._MAX_SPY_TOOLS]:
            if not isinstance(tool, dict):
                continue
            sanitized = {
                "tool_call_id": cls._truncate_spy_text(tool.get("tool_call_id")),
                "tool": cls._truncate_spy_text(tool.get("tool")),
                "status": cls._truncate_spy_text(tool.get("status")),
                "started_at": cls._truncate_spy_text(tool.get("started_at")),
                "ended_at": cls._truncate_spy_text(tool.get("ended_at")),
                "duration_ms": tool.get("duration_ms"),
                "input": cls._sanitize_spy_map(tool.get("input"), max_items=4),
                "output_meta": cls._sanitize_spy_map(tool.get("output_meta"), max_items=4),
                "error": cls._sanitize_spy_map(tool.get("error"), max_items=2),
            }
            sanitized_tools.append(sanitized)

        return {
            "turn_id": cls._truncate_spy_text(detail.get("turn_id")),
            "tools": sanitized_tools,
            "truncated_tools": len(tools) > cls._MAX_SPY_TOOLS,
        }

    def _update_spy_telemetry(self, agent: str) -> None:
        agent_client = self._get_agent_client()
        if agent_client is None:
            return
        detail = self._sanitize_spy_turn_detail(getattr(agent_client, "last_spy_turn_detail", None))
        if detail is None:
            return
        snapshot = {
            "agent": agent,
            "captured_at": int(time.time()),
            "turn_detail": detail,
        }
        shared_state = self._shared_state()
        if isinstance(shared_state, dict):
            shared_state_lock = self._shared_state_lock()
            with (shared_state_lock if shared_state_lock is not None else nullcontext()):
                shared_state["spy_last_turn_detail"] = snapshot
        session_meta = self._session_meta()
        if isinstance(session_meta, dict):
            counter_lock = self._call(self._counter_lock)
            with (counter_lock if counter_lock is not None else nullcontext()):
                session_meta["last_spy_turn_detail"] = snapshot

    # -------------------------------------------------------------------------
    # API pública
    # -------------------------------------------------------------------------

    def resolve_agent_response(
            self,
            agent: str,
            response: str | None,
            silent: bool = False,
            persist_history: bool = True,
            show_output: bool = True,
    ) -> str | None:
        """Resolve respostas com loop de ferramentas até estabilizar a saída."""
        return self._get_tool_loop().execute(
            agent, response, silent=silent, persist_history=persist_history, show_output=show_output
        )

    def call_agent(self, agent, **options):
        """Executa despacho com retry e resolução de ferramentas."""
        dispatch_options = dict(options)
        silent = dispatch_options.pop("silent", False)
        persist_history = dispatch_options.pop("persist_history", True)
        show_output = dispatch_options.pop("show_output", True)
        dispatch_options.pop("quiet", False)
        dispatch_options.pop("progress_callback", None)
        handoff = dispatch_options.get("handoff")
        handoff_id = handoff.get("handoff_id") if isinstance(handoff, dict) else None
        logger.debug(
            "[DISPATCH] sending to agent=%s, handoff_only=%s, handoff_id=%s",
            agent, dispatch_options.get("handoff_only", False), handoff_id,
        )
        agent_client = self._get_agent_client()
        if agent_client is not None and hasattr(agent_client, "execution_mode"):
            agent_client.execution_mode = self._get_execution_mode() if self._get_execution_mode else None
        service = self._get_agent_call_service()

        def _call_fn(a):
            return self.call_agent_low_level(
                a, silent=silent, show_output=show_output, **dispatch_options,
            )

        def _resolve_fn(a, response):
            return self.resolve_agent_response(
                a, response, silent=silent, persist_history=persist_history, show_output=show_output,
            )

        return service.call(
            agent=agent,
            call_fn=_call_fn,
            resolve_fn=_resolve_fn,
            is_user_cancelled=(
                (lambda: bool(self._cancel_checker_override()))
                if callable(self._cancel_checker_override)
                else (lambda: _is_user_cancelled(agent_client))
            ),
        )

    def call_agent_low_level(
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
    ):
        """Monta o prompt final e executa a chamada ao backend do agente."""
        result = self._get_gateway().call(
            agent,
            is_first_speaker=is_first_speaker,
            handoff=handoff,
            primary=primary,
            protocol_mode=protocol_mode,
            handoff_only=handoff_only,
            silent=silent,
            show_output=show_output,
            from_agent=from_agent,
            prompt_kind=prompt_kind,
        )
        self._update_spy_telemetry(agent)
        return result

    def print_response(self, agent, response):
        """Exibe saída do agente preservando o prompt não bloqueante."""
        if self._ui_queue is not None:
            self._ui_queue.put(
                RenderEvent(
                    RenderEvent.TEXT,
                    response if response is not None else "",
                    agent=agent,
                    metadata={"no_response": response is None},
                )
            )
            return
        # fallback — comportamento original
        output_lock = self._call(self._output_lock)
        clear_prompt_line = self._clear_prompt_line
        redisplay_prompt = self._redisplay_prompt
        renderer = self._call(self._renderer)
        with (output_lock if output_lock is not None else nullcontext()):
            if clear_prompt_line is not None:
                clear_prompt_line()
            if response is not None:
                show_message = getattr(renderer, "show_message", None) if renderer else None
                if callable(show_message):
                    show_message(agent, response)
            else:
                show_no_response = getattr(renderer, "show_no_response", None) if renderer else None
                if callable(show_no_response):
                    show_no_response(agent)
            flush = getattr(renderer, "flush", None) if renderer else None
            if callable(flush):
                flush()
            if redisplay_prompt is not None:
                redisplay_prompt(clear_first=False)

    def close(self) -> None:
        """Libera recursos do agent client dedicado quando houver override."""
        agent_client = self._agent_client_override
        if agent_client is None:
            return
        close = getattr(agent_client, "close", None)
        if callable(close):
            close()
