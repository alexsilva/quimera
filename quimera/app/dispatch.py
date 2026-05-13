"""Componentes de `quimera.app.dispatch`."""
import time
from contextlib import nullcontext

from .agent_call_service import AgentCallService
from .agent_gateway import AgentGateway, _is_user_cancelled
from .tool_loop import ToolLoopService
from .config import logger


class AppDispatchServices:
    """Coordena AgentGateway e ToolLoopService; mantém spy telemetry."""

    _MAX_SPY_TOOLS = 12
    _MAX_SPY_TEXT_CHARS = 280
    _MAX_SPY_MAP_ITEMS = 6

    def __init__(self, app):
        """Inicializa uma instância de AppDispatchServices."""
        self.app = app
        # _gateway e _tool_loop são construídos lazily porque app ainda não tem
        # todos os atributos inicializados quando AppDispatchServices é criado.
        self._gateway = None
        self._tool_loop = None
        self._agent_call_service = None

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
        app = self.app
        counter_lock = getattr(app, "_counter_lock", None)

        def _increment_call_index():
            with (counter_lock if counter_lock is not None else nullcontext()):
                app.session_call_index += 1
                return app.session_call_index

        def _update_session(agent: str, success: bool, elapsed: float):
            if not (hasattr(app, "session_state") and app.session_state):
                return
            with (counter_lock if counter_lock is not None else nullcontext()):
                try:
                    app.session_state["handoffs_sent"] += 1
                    app.session_state["total_latency"] += elapsed
                    if success:
                        app.session_state["handoffs_succeeded"] += 1
                    else:
                        app.session_state["handoffs_failed"] += 1
                except KeyError:
                    pass
            session_metrics = getattr(app, "session_metrics", None)
            if session_metrics is not None:
                session_metrics.record_agent_metric(app, agent, "succeeded" if success else "failed", elapsed)

        return AgentGateway(
            agent_client=app.agent_client,
            prompt_builder=app.prompt_builder,
            renderer=app.renderer,
            plugin_resolver=app.get_agent_plugin,
            get_history=lambda: app.history,
            get_shared_state=lambda: app.shared_state,
            get_execution_mode=lambda: getattr(app, "execution_mode", None),
            refresh_task_state=app.task_services.refresh_task_shared_state,
            session_state=app.session_state,
            increment_call_index=_increment_call_index,
            get_round_index=lambda: getattr(app, "round_index", 0),
            debug_prompt_metrics=getattr(app, "debug_prompt_metrics", False),
            clear_prompt_line=getattr(app, "_clear_user_prompt_line_if_needed", None),
            redisplay_prompt=getattr(app, "_redisplay_user_prompt_if_needed", None),
            update_session=_update_session,
            output_lock=getattr(app, "_output_lock", None),
            counter_lock=counter_lock,
        )

    def _build_tool_loop(self) -> ToolLoopService:
        app = self.app

        def _cancel_checker() -> bool:
            return _is_user_cancelled(getattr(app, "agent_client", None))

        def _call_agent_fn(agent, **kwargs):
            if hasattr(app, "_call_agent"):
                return app._call_agent(agent, **kwargs)
            return self.call_agent_low_level(agent, **kwargs)

        def _record_tool_event(agent, **kwargs):
            session_metrics = getattr(app, "session_metrics", None)
            if session_metrics is not None:
                session_metrics.record_tool_event(app, agent, **kwargs)

        def _reset_approve_all():
            tool_executor = getattr(app, "tool_executor", None)
            if tool_executor is None:
                return
            approval_handler = getattr(tool_executor, "approval_handler", None)
            if approval_handler is not None and hasattr(approval_handler, "reset_approve_all_after_cycle"):
                approval_handler.reset_approve_all_after_cycle()

        def _progress_callback(agent, tool_name, hop, max_hops, elapsed, ok, is_invalid):
            renderer = getattr(app, "renderer", None)
            if renderer is not None:
                status = "✓" if ok else "✗" if is_invalid else "○"
                show_system_neutral = getattr(renderer, "show_system_neutral", None)
                if callable(show_system_neutral):
                    show_system_neutral(
                        f"[tool hop {hop}/{max_hops}] {agent} {status} {tool_name}"
                    )

        return ToolLoopService(
            tool_executor=app.tool_executor,
            plugin_resolver=app.get_agent_plugin,
            call_agent_fn=_call_agent_fn,
            print_response_fn=app.print_response,
            persist_message_fn=lambda agent, text: app.session_services.persist_message(agent, text),
            cancel_checker=_cancel_checker,
            record_tool_event=_record_tool_event,
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
        app = self.app
        agent_client = getattr(app, "agent_client", None)

        def _is_rate_limited():
            return bool(agent_client and getattr(agent_client, 'rate_limit_detected', False))

        return AgentCallService(
            max_retries=getattr(app, "MAX_RETRIES", 2),
            retry_backoff=getattr(app, "RETRY_BACKOFF_SECONDS", 1),
            rate_limit_backoff=getattr(app, "RATE_LIMIT_BACKOFF_SECONDS", 30),
            record_failure=getattr(app, "record_failure", None),
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
        app = self.app
        agent_client = getattr(app, "agent_client", None)
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
        shared_state = getattr(app, "shared_state", None)
        if isinstance(shared_state, dict):
            shared_state_lock = getattr(app, "_shared_state_lock", None)
            with (shared_state_lock if shared_state_lock is not None else nullcontext()):
                shared_state["spy_last_turn_detail"] = snapshot
        session_state = getattr(app, "session_state", None)
        if isinstance(session_state, dict):
            counter_lock = getattr(app, "_counter_lock", None)
            with (counter_lock if counter_lock is not None else nullcontext()):
                session_state["last_spy_turn_detail"] = snapshot

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
        app = self.app
        dispatch_options = dict(options)
        silent = dispatch_options.pop("silent", False)
        persist_history = dispatch_options.pop("persist_history", True)
        show_output = dispatch_options.pop("show_output", True)
        dispatch_options.pop("quiet", False)
        dispatch_options.pop("progress_callback", None)
        handoff = dispatch_options.get("handoff")
        handoff_id = handoff.get("handoff_id") if isinstance(handoff, dict) else None
        logger.info(
            "[DISPATCH] sending to agent=%s, handoff_only=%s, handoff_id=%s",
            agent, dispatch_options.get("handoff_only", False), handoff_id,
        )
        agent_client = getattr(app, "agent_client", None)
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
            is_user_cancelled=lambda: _is_user_cancelled(agent_client),
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
        )
        self._update_spy_telemetry(agent)
        return result

    def print_response(self, agent, response):
        """Exibe saída do agente preservando o prompt não bloqueante."""
        app = self.app
        output_lock = getattr(app, "_output_lock", None)
        with (output_lock if output_lock is not None else nullcontext()):
            if hasattr(app, "_clear_user_prompt_line_if_needed"):
                app._clear_user_prompt_line_if_needed()
            if response is not None:
                app.renderer.show_message(agent, response)
            else:
                app.renderer.show_no_response(agent)
            flush = getattr(app.renderer, "flush", None)
            if callable(flush):
                flush()
            if hasattr(app, "_redisplay_user_prompt_if_needed"):
                app._redisplay_user_prompt_if_needed(clear_first=False)
