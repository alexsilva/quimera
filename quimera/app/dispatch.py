"""Componentes de `quimera.app.dispatch`."""
import copy
import queue as _queue_module
import time
from contextlib import nullcontext

from ..prompt_kinds import PromptKind
from .agent_call_service import AgentCallService
from .agent_gateway import AgentGateway, _is_user_cancelled
from .render_event import RenderEvent
from .config import logger
from ..domain.session_state import SessionState


class AppDispatchServices:
    """Coordena AgentGateway e mantém spy telemetry."""

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
        get_agent_profile=None,
        session_state: "SessionState | None" = None,
        get_execution_mode=None,
        refresh_task_state=None,
        debug_prompt_metrics=False,
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
        get_delegate_fn_override=None,
        # compat: aceita lambdas individuais quando session_state não é fornecido
        get_history=None,
        get_shared_state=None,
        get_session_state=None,
        get_round_index=None,
        get_session_call_index=None,
        set_session_call_index=None,
        get_shared_state_lock=None,
        notify_warning=None,
        notify_retry=None,
        notify_error=None,
        agent_run_sink=None,
    ):
        self._agent_client_override = agent_client_override
        self._tool_executor_override = tool_executor_override
        self._cancel_checker_override = cancel_checker_override
        self._ui_queue = ui_queue
        self._prompt_builder = prompt_builder
        self._renderer = renderer
        self._get_agent_profile = get_agent_profile
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
        self._get_delegate_fn_override = get_delegate_fn_override
        self._notify_warning = notify_warning
        self._notify_retry = notify_retry
        self._notify_error = notify_error
        self._agent_run_sink = agent_run_sink
        self._gateway = None
        self._agent_call_service = None

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

    def _show_delegation(
            self,
            from_agent: str | None,
            to_agent: str,
            task: str | None,
            *,
            delegation_id: str | None = None,
            chain: list | tuple | None = None,
    ) -> None:
        """Exibe delegação no renderer atual, preservando compatibilidade sem Textual."""
        metadata = {
            "to": to_agent,
            "task": task,
            "delegation_id": delegation_id,
            "chain": list(chain or []),
        }
        source_agent = str(from_agent or "quimera")
        if self._ui_queue is not None:
            self._ui_queue.put(
                RenderEvent(
                    RenderEvent.DELEGATION,
                    "",
                    agent=source_agent,
                    metadata=metadata,
                )
            )
            return
        renderer = self._call(self._renderer)
        show_delegation = getattr(renderer, "show_delegation", None) if renderer is not None else None
        if callable(show_delegation):
            show_delegation(
                source_agent,
                to_agent,
                task=task,
                delegation_id=delegation_id,
                chain=metadata["chain"],
            )

    # -------------------------------------------------------------------------
    # Lazy builders
    # -------------------------------------------------------------------------

    def _get_gateway(self) -> AgentGateway:
        if self._gateway is None:
            self._gateway = self._build_gateway()
        return self._gateway

    def _build_gateway(self) -> AgentGateway:
        prompt_builder = self._call(self._prompt_builder)
        renderer = self._call(self._renderer)
        profile_resolver = self._get_agent_profile
        refresh_task_state = self._refresh_task_state
        debug_prompt_metrics = self._call(self._debug_prompt_metrics)
        redisplay_prompt = self._redisplay_prompt
        output_lock = self._call(self._output_lock)
        counter_lock = self._call(self._counter_lock)
        session_state = self._session_state

        def _update_session(agent: str, success: bool, elapsed: float):
            ss = self._session_meta()
            if not ss:
                return
            if session_state is not None and hasattr(session_state, "record_delegation"):
                session_state.record_delegation(success)
                ss["total_latency"] = ss.get("total_latency", 0.0) + elapsed
                if self._record_session_metric:
                    self._record_session_metric(agent, "succeeded" if success else "failed", elapsed)
                return
            try:
                ss["delegations_sent"] += 1
                ss["total_latency"] += elapsed
                if success:
                    ss["delegations_succeeded"] += 1
                else:
                    ss["delegations_failed"] += 1
            except KeyError:
                pass
            if self._record_session_metric:
                self._record_session_metric(agent, "succeeded" if success else "failed", elapsed)

        return AgentGateway(
            agent_client=self._get_agent_client(),
            prompt_builder=prompt_builder,
            renderer=renderer,
            profile_resolver=profile_resolver,
            session_state=session_state,
            get_history=self._history,
            get_shared_state=self._shared_state,
            get_execution_mode=self._get_execution_mode,
            refresh_task_state=refresh_task_state,
            increment_call_index=self._increment_call_index,
            get_round_index=self._round_index,
            debug_prompt_metrics=debug_prompt_metrics,
            redisplay_prompt=redisplay_prompt,
            update_session=_update_session,
            output_lock=output_lock,
            counter_lock=counter_lock,
            ui_queue=self._ui_queue,
            agent_run_sink=self._call(self._agent_run_sink),
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
        renderer = self._call(self._renderer)

        def _is_rate_limited():
            return bool(agent_client and getattr(agent_client, 'rate_limit_detected', False))

        def _before_retry(agent: str, attempt: int, reason: str) -> None:
            del agent, attempt, reason
            if renderer is None:
                return
            flush_quick = getattr(renderer, "flush_quick", None)
            if callable(flush_quick):
                try:
                    flush_quick(timeout=0.25)
                except TypeError:
                    flush_quick()

        return AgentCallService(
            max_retries=self._call(self._max_retries),
            retry_backoff=self._call(self._retry_backoff),
            rate_limit_backoff=self._call(self._rate_limit_backoff),
            record_failure=self._record_failure,
            record_success=self._record_success,
            is_rate_limited=_is_rate_limited,
            before_retry=_before_retry,
            notify_warning=self._notify_warning,
            notify_retry=self._notify_retry,
            notify_error=self._notify_error,
        )

    # -------------------------------------------------------------------------
    # Spy telemetry (permanece em AppDispatchServices)
    # -------------------------------------------------------------------------

    @classmethod
    def _truncate_spy_text(cls, value):
        # Mantido por compatibilidade; não truncamos mais para preservar evidência completa.
        return value

    @classmethod
    def _sanitize_spy_map(cls, payload, max_items=None):
        if not isinstance(payload, dict):
            return None
        return copy.deepcopy(payload) or None

    @classmethod
    def _sanitize_spy_turn_detail(cls, detail):
        if not isinstance(detail, dict):
            return None

        sanitized = copy.deepcopy(detail)
        tools = sanitized.get("tools")
        if not isinstance(tools, list):
            sanitized["tools"] = []
        else:
            sanitized["tools"] = [tool for tool in tools if isinstance(tool, dict)]
        sanitized["truncated_tools"] = False
        return sanitized

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
        """Retorna a resposta do agente para o fluxo padrão da aplicação.

        A execução de ferramentas pertence aos caminhos estruturados do runtime:
        MCP e tool calling nativo do driver OpenAI-compatible. Os parâmetros são
        preservados por compatibilidade com call sites.
        """
        return response

    def delegate(self, agent, **options):
        """Executa despacho com retry e finalização padrão da resposta."""
        delegate_fn_override = self._call(self._get_delegate_fn_override)
        if delegate_fn_override is not None:
            return delegate_fn_override(agent, **options)
        dispatch_options = dict(options)
        max_retries_override = dispatch_options.pop("max_retries", None)
        silent = dispatch_options.pop("silent", False)
        persist_history = dispatch_options.pop("persist_history", True)
        show_output = dispatch_options.pop("show_output", True)
        dispatch_options.pop("quiet", False)
        progress_callback = dispatch_options.pop("progress_callback", None)
        delegation = dispatch_options.get("delegation")
        delegation_id = delegation.get("delegation_id") if isinstance(delegation, dict) else None
        from_agent = dispatch_options.get("from_agent")
        if isinstance(delegation, dict):
            self._show_delegation(
                str(from_agent or delegation.get("from_agent") or "quimera"),
                str(agent),
                delegation.get("task"),
                delegation_id=delegation_id,
                chain=delegation.get("chain") if isinstance(delegation.get("chain"), (list, tuple)) else None,
            )
        logger.debug(
            "[DISPATCH] sending to agent=%s, delegation_only=%s, delegation_id=%s",
            agent, dispatch_options.get("delegation_only", False), delegation_id,
        )
        agent_client = self._get_agent_client()
        if agent_client is not None and hasattr(agent_client, "execution_mode"):
            agent_client.execution_mode = self._get_execution_mode() if self._get_execution_mode else None
        service = self._get_agent_call_service()

        def _call_fn(a):
            return self.delegate_low_level(
                a,
                silent=silent,
                show_output=show_output,
                progress_callback=progress_callback,
                **dispatch_options,
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
            max_retries=max_retries_override,
        )

    def delegate_low_level(
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
        result = self._get_gateway().call(
            agent,
            is_first_speaker=is_first_speaker,
            delegation=delegation,
            primary=primary,
            protocol_mode=protocol_mode,
            delegation_only=delegation_only,
            silent=silent,
            show_output=show_output,
            from_agent=from_agent,
            prompt_kind=prompt_kind,
            history_snapshot=history_snapshot,
            request_override=request_override,
            progress_callback=progress_callback,
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
        redisplay_prompt = self._redisplay_prompt
        renderer = self._call(self._renderer)
        with (output_lock if output_lock is not None else nullcontext()):
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
