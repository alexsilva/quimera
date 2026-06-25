"""Orquestra uma rodada de chat multiagente."""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from typing import Any

from ..constants import MSG_EMPTY_INPUT, USER_ROLE
from .config import logger
from .render_event import RenderEvent
from ..domain.session_state import SessionState


@dataclass(frozen=True)
class ChatRoundContext:
    """Contexto explícito de dependências injetáveis na rodada de chat."""

    session_services: Any | None = None
    task_services: Any | None = None
    renderer: Any | None = None
    session_state: SessionState | dict | None = None
    parse_routing: Any | None = None
    parse_response: Any | None = None
    dispatch_services: Any | None = None
    show_system_message: Any | None = None
    ui_queue: Any | None = None


class ChatRoundOrchestrator:
    """Executa o fluxo completo de uma rodada de chat."""

    def __init__(
        self,
        dispatch_services=None,
        parse_routing=None,
        agent_pool=None,
        session_services=None,
        parse_response=None,
        *,
        agent_client=None,
        turn_manager=None,
        task_services=None,
        get_agent_profile=None,
        behavior_metrics=None,
        threads=1,
        session_state=None,
        show_system_message=None,
        renderer=None,
        ui_queue=None,
        merge_staging_to_workspace=None,
        set_parallel_toolbar_state=None,
        # compat: aceita lambdas individuais quando session_state não é SessionState
        get_round_index=None,
        set_round_index=None,
        set_summary_agent_preference=None,
        get_pending_input_for=None,
        set_pending_input_for=None,
    ):
        if parse_routing is None and hasattr(dispatch_services, "parse_routing"):
            app = dispatch_services
            dispatch_services = getattr(app, "dispatch_services", None)
            parse_routing = lambda user: app.parse_routing(user)
            agent_pool = getattr(app, "agent_pool", None)
            if agent_pool is None:
                from .agent_pool import AgentPool
                agent_pool = AgentPool(getattr(app, "active_agents", []) or [])
            session_services = getattr(app, "session_services", None)
            parse_response = lambda response: app.parse_response(response)
            agent_client = getattr(app, "agent_client", agent_client)
            turn_manager = getattr(app, "turn_manager", turn_manager)
            task_services = getattr(app, "task_services", task_services)
            get_agent_profile = getattr(app, "get_agent_profile", get_agent_profile)
            behavior_metrics = getattr(app, "behavior_metrics", behavior_metrics)
            threads = getattr(app, "threads", threads)
            session_state = getattr(app, "_chat_state", getattr(app, "session_state", session_state))
            show_system_message = getattr(getattr(app, "system_layer", None), "show_system_message", show_system_message)
            renderer = getattr(app, "renderer", renderer)
            ui_queue = getattr(app, "_ui_event_queue", ui_queue)
            if not isinstance(session_state, SessionState):
                get_round_index = get_round_index or (lambda: getattr(app, "round_index", 0))
                set_round_index = set_round_index or (lambda value: setattr(app, "round_index", value))
                set_summary_agent_preference = set_summary_agent_preference or (
                    lambda value: setattr(app, "summary_agent_preference", value)
                )
                set_parallel_toolbar_state = set_parallel_toolbar_state or getattr(
                    app, "_set_parallel_toolbar_state", None
                )
                get_pending_input_for = get_pending_input_for or (
                    lambda: getattr(app, "_pending_input_for", None)
                )
                set_pending_input_for = set_pending_input_for or (
                    lambda value: setattr(app, "_pending_input_for", value)
                )

        self._dispatch_services = dispatch_services
        self._parse_routing = parse_routing
        self._agent_pool = agent_pool
        self._session_services = session_services
        self._parse_response = parse_response
        self._agent_client = agent_client
        self._turn_manager = turn_manager
        self._task_services = task_services
        self._get_agent_profile = get_agent_profile
        self._behavior_metrics = behavior_metrics
        self._threads = threads
        # session_state pode ser SessionState ou dict legado
        self._session_state = session_state if isinstance(session_state, SessionState) else None
        self._session_state_dict = session_state if not isinstance(session_state, SessionState) else None
        # compat: lambdas individuais (usadas quando _session_state é None)
        self._get_round_index_fn = get_round_index or (lambda: 0)
        self._set_round_index_fn = set_round_index
        self._set_summary_agent_preference_fn = set_summary_agent_preference
        self._set_parallel_toolbar_state_fn = set_parallel_toolbar_state
        self._get_pending_input_for_fn = get_pending_input_for or (lambda: None)
        self._set_pending_input_for_fn = set_pending_input_for
        self._show_system_message = show_system_message
        self._renderer = renderer
        self._ui_queue = ui_queue
        self._merge_staging_to_workspace_fn = merge_staging_to_workspace
        self._persist_message_supports_snapshot: bool | None = None
        self._cancel_notice_tls = threading.local()

    # ------------------------------------------------------------------
    # Accessors de estado — preferem SessionState, caem nos lambdas legados
    # ------------------------------------------------------------------

    def _get_round_index(self) -> int:
        if self._session_state is not None:
            return self._session_state.round_index
        return self._get_round_index_fn()

    def _set_round_index(self, value: int) -> None:
        if self._session_state is not None:
            self._session_state.round_index = value
        elif self._set_round_index_fn is not None:
            self._set_round_index_fn(value)

    def _set_summary_agent_preference(self, value: str | None) -> None:
        if self._session_state is not None:
            self._session_state.summary_agent_preference = value
        elif self._set_summary_agent_preference_fn is not None:
            self._set_summary_agent_preference_fn(value)

    def _get_pending_input_for(self) -> str | None:
        if self._session_state is not None:
            return self._session_state.pending_input_for
        return self._get_pending_input_for_fn()

    def _set_pending_input_for(self, value: str | None) -> None:
        if self._session_state is not None:
            self._session_state.pending_input_for = value
        elif self._set_pending_input_for_fn is not None:
            self._set_pending_input_for_fn(value)

    def _handle_needs_human_input(self, agent: str) -> None:
        """Em paralelo, não força binding de resposta para um único agente."""
        if self._threads > 1:
            return
        self._set_pending_input_for(agent)
        self._show_system(f"Responda para {agent.upper()}:")

    def _snapshot_history(self) -> list:
        history = None
        if self._session_state is not None:
            history = self._session_state.history
        elif isinstance(self._session_state_dict, dict):
            history = self._session_state_dict.get("history")
        if history is None:
            return []
        if isinstance(history, list):
            return list(history)
        try:
            return list(history)
        except Exception:
            return []

    def _can_request_history_snapshot(self) -> bool:
        if self._persist_message_supports_snapshot is not None:
            return self._persist_message_supports_snapshot
        persist_message = getattr(self._session_services, "persist_message", None)
        if not callable(persist_message):
            self._persist_message_supports_snapshot = False
            return False
        try:
            signature = inspect.signature(persist_message)
        except (TypeError, ValueError):
            self._persist_message_supports_snapshot = False
            return False
        self._persist_message_supports_snapshot = "return_history_snapshot" in signature.parameters
        return self._persist_message_supports_snapshot

    def _persist_user_message(self, message: str) -> list:
        if self._can_request_history_snapshot():
            history_snapshot = self._session_services.persist_message(
                USER_ROLE,
                message,
                return_history_snapshot=True,
            )
            if isinstance(history_snapshot, list):
                return history_snapshot
        else:
            self._session_services.persist_message(USER_ROLE, message)
        return self._snapshot_history()

    @staticmethod
    def _filter_supported_kwargs(func, kwargs: dict) -> dict:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return kwargs
        has_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if has_var_kwargs:
            return kwargs
        allowed = {
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }
        return {key: value for key, value in kwargs.items() if key in allowed}

    def _delegate(self, agent: str, **kwargs):
        if self._threads > 1 and "max_retries" not in kwargs:
            # Em modo threaded cada prompt deve executar uma vez por agente;
            # retries automáticos causam efeito de loop/cascata no chat.
            kwargs["max_retries"] = 1
        delegate_fn = self._dispatch_services.delegate
        filtered_kwargs = self._filter_supported_kwargs(delegate_fn, kwargs)
        return delegate_fn(agent, **filtered_kwargs)

    def _set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if self._set_parallel_toolbar_state_fn is None:
            return
        try:
            self._set_parallel_toolbar_state_fn(
                active=active,
                queued=queued,
                capacity=capacity,
                active_agents=active_agents,
            )
        except AttributeError:
            return

    def _emit_event(
        self,
        event_type: str,
        payload,
        *,
        agent: str | None = None,
        metadata: dict | None = None,
    ) -> bool:
        if self._ui_queue is None:
            return False
        self._ui_queue.put(RenderEvent(event_type, payload, agent=agent, metadata=metadata))
        return True

    def _show_system(self, message: str) -> None:
        """Exibe mensagem de sistema via camada que preserva prompt quando disponível."""
        if self._emit_event(RenderEvent.SYSTEM, message):
            return
        show_system_message = self._show_system_message
        if callable(show_system_message):
            try:
                show_system_message(message)
                return
            except AttributeError:
                pass
        if self._renderer is not None:
            show_system = getattr(self._renderer, "show_system", None)
            if callable(show_system):
                show_system(message)

    def _show_warning(self, message: str) -> None:
        if self._emit_event(RenderEvent.WARNING, message):
            return
        if self._renderer is not None:
            show_warning = getattr(self._renderer, "show_warning", None)
            if callable(show_warning):
                show_warning(message)

    def _show_agent_message(self, agent: str, message: str | None) -> None:
        if self._emit_event(
            RenderEvent.TEXT,
            message if message is not None else "",
            agent=agent,
            metadata={"no_response": message is None},
        ):
            return
        if self._renderer is None:
            return
        if message is None:
            show_no_response = getattr(self._renderer, "show_no_response", None)
            if callable(show_no_response):
                show_no_response(agent)
            return
        show_message = getattr(self._renderer, "show_message", None)
        if callable(show_message):
            show_message(agent, message)

    def _show_delegation(self, from_agent: str, to_agent: str, task: str | None) -> None:
        if self._emit_event(
            RenderEvent.DELEGATION,
            "",
            agent=from_agent,
            metadata={"to": to_agent, "task": task},
        ):
            return
        if self._renderer is not None:
            show_delegation = getattr(self._renderer, "show_delegation", None)
            if callable(show_delegation):
                show_delegation(from_agent, to_agent, task=task)

    def _is_cancelled(self) -> bool:
        return bool(self._agent_client and getattr(self._agent_client, '_user_cancelled', False))

    def _handle_cancelled(self) -> None:
        if bool(getattr(self._cancel_notice_tls, "shown", False)):
            return
        self._cancel_notice_tls.shown = True
        if self._turn_manager is not None:
            self._turn_manager.reset()

    def _apply_runtime_context(self, ctx: ChatRoundContext) -> None:
        if ctx.session_services is not None:
            self._session_services = ctx.session_services
        if ctx.task_services is not None:
            self._task_services = ctx.task_services
        if ctx.renderer is not None:
            self._renderer = ctx.renderer
        if ctx.session_state is not None:
            if isinstance(ctx.session_state, SessionState):
                self._session_state = ctx.session_state
                self._session_state_dict = None
            else:
                self._session_state = None
                self._session_state_dict = ctx.session_state
        if ctx.parse_routing is not None:
            self._parse_routing = ctx.parse_routing
        if ctx.parse_response is not None:
            self._parse_response = ctx.parse_response
        if ctx.dispatch_services is not None:
            self._dispatch_services = ctx.dispatch_services
        if ctx.show_system_message is not None:
            self._show_system_message = ctx.show_system_message
        self._ui_queue = ctx.ui_queue

    def process(self, user, *, ctx: ChatRoundContext | None = None):
        """Implementação real do processamento de mensagens do chat."""
        if ctx is not None:
            self._apply_runtime_context(ctx)
        self._cancel_notice_tls.shown = False
        self._set_parallel_toolbar_state(
            active=0,
            queued=0,
            capacity=max(0, self._threads),
            active_agents=(),
        )
        first_agent, message, explicit = self._parse_routing(user)
        if first_agent is None:
            return
        if not message or not message.strip():
            self._show_warning(MSG_EMPTY_INPUT.format(first_agent))
            return

        pending_input_for = self._get_pending_input_for()
        if pending_input_for and not explicit:
            first_agent = pending_input_for
        elif not explicit and self._agent_pool is not None:
            reserved_agent = self._agent_pool.take_primary()
            if reserved_agent is not None:
                first_agent = reserved_agent
        self._set_pending_input_for(None)

        self._set_round_index(self._get_round_index() + 1)
        self._set_summary_agent_preference(first_agent)
        history_snapshot = self._persist_user_message(message)
        prompt_binding = {"request_override": message}
        if history_snapshot:
            prompt_binding["history_snapshot"] = history_snapshot

        response = self._delegate(
            first_agent,
            is_first_speaker=True,
            protocol_mode="standard",
            **prompt_binding,
        )
        response, _, _, extend, needs_human_input, _ = self._parse_response(response)

        if self._is_cancelled():
            self._handle_cancelled()
            return

        if response is None and not needs_human_input:
            if self._is_cancelled():
                self._handle_cancelled()
                return

            fallback_candidates = [agent for agent in self._agent_pool.agents if agent != first_agent]
            failed_agent = first_agent
            for fallback_agent in fallback_candidates:
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                logger.debug(
                    "no response from %s; failover to %s",
                    failed_agent, fallback_agent,
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                self._show_system(
                    f"{failed_agent} não respondeu, tentando com {fallback_agent}"
                )
                fallback_response = self._delegate(
                    fallback_agent,
                    is_first_speaker=True,
                    primary=False,
                    protocol_mode="standard",
                    **prompt_binding,
                )
                fallback_response, _, _, extend, needs_human_input, _ = self._parse_response(
                    fallback_response
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                if fallback_response is None and not needs_human_input:
                    failed_agent = fallback_agent
                    continue
                first_agent = fallback_agent
                self._set_summary_agent_preference(first_agent)
                response = fallback_response
                break

            if response is None and not needs_human_input:
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                self._show_warning("Nenhum agente disponível respondeu.")
                return

        if needs_human_input:
            if response:
                self._show_agent_message(first_agent, response)
            self._handle_needs_human_input(first_agent)
            return
        other_agents = [agent for agent in self._agent_pool.agents if agent != first_agent]
        self._dispatch_services.print_response(first_agent, response)
        if response is not None:
            self._session_services.persist_message(first_agent, response)

        self._process_standard_flow(
            first_agent,
            explicit,
            extend,
            other_agents,
            request_override=message,
            history_snapshot=history_snapshot if history_snapshot else None,
        )

        if self._get_pending_input_for() is not None:
            return

        self._session_services.maybe_auto_summarize(preferred_agent=first_agent)

    def _process_standard_flow(
        self,
        first_agent,
        explicit,
        extend,
        other_agents,
        *,
        request_override: str | None = None,
        history_snapshot: list | None = None,
    ):
        protocol_mode = "extended" if extend else "standard"
        call_prompt_binding = {}
        if request_override:
            call_prompt_binding["request_override"] = request_override
        if history_snapshot:
            call_prompt_binding["history_snapshot"] = history_snapshot
        parallel_slots = max(0, self._threads)
        self._set_parallel_toolbar_state(
            active=0,
            queued=0,
            capacity=parallel_slots,
            active_agents=(),
        )
        if extend and not explicit and self._task_services is not None:
            logger.info(
                "[standard-flow] extend marker received; running sequential follow-up from %s",
                first_agent,
            )
            if other_agents:
                self._show_system(f"[debate] iniciado: {first_agent} ↔ {other_agents[0]}")
            else:
                self._show_system(f"[debate] iniciado: {first_agent}")
            remaining = [other_agents[0], first_agent, other_agents[0]] if other_agents else []
            for index, agent in enumerate(remaining):
                self._set_parallel_toolbar_state(
                    active=0,
                    queued=max(0, len(remaining) - (index + 1)),
                    capacity=parallel_slots,
                    active_agents=(),
                )
                response = self._delegate(
                    agent,
                    primary=False,
                    protocol_mode=protocol_mode,
                    **call_prompt_binding,
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                response, _, _, _, needs_human_input, _ = self._parse_response(response)
                self._dispatch_services.print_response(agent, response)
                if response is not None:
                    self._session_services.persist_message(agent, response)
                if needs_human_input:
                    self._handle_needs_human_input(agent)
                    break
            if self._get_pending_input_for() is None:
                self._set_parallel_toolbar_state(
                    active=0,
                    queued=0,
                    capacity=parallel_slots,
                    active_agents=(),
                )
            return
        if extend:
            logger.info(
                "[standard-flow] extend marker ignored in chat round; same prompt stays bound to %s",
                first_agent,
            )
        if self._get_pending_input_for() is None:
            self._set_parallel_toolbar_state(
                active=0,
                queued=0,
                capacity=parallel_slots,
                active_agents=(),
            )
