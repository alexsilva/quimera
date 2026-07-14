"""Orquestra uma rodada de chat multiagente."""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from typing import Any

from ..constants import MSG_EMPTY_INPUT, USER_ROLE
from .command_router import RoutingDecision
from .config import logger
from .render_event import RenderEvent
from ..domain.session_state import SessionState
from ..ui.messages import format_failover_message


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
    ):
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
            self._renderer.show_system(message)

    def _notify_failover(self, agent: str, target: str) -> None:
        """Sinaliza failover de agente como atividade estruturada.

        Transporta os campos separados (agente que falhou, alvo) em vez de uma
        frase reparseável. Cai em texto de sistema quando não há renderer com
        canal estruturado.
        """
        if self._emit_event(
            RenderEvent.AGENT_ACTIVITY,
            None,
            agent=agent,
            metadata={"activity": "failover", "target": target},
        ):
            return
        if self._renderer is not None:
            self._renderer.notify_agent_failover(agent, target=target)
            return
        self._show_system(format_failover_message(agent, target))

    def _show_warning(self, message: str) -> None:
        if self._emit_event(RenderEvent.WARNING, message):
            return
        if self._renderer is not None:
            self._renderer.show_warning(message)

    def _show_delegation(
            self,
            from_agent: str,
            to_agent: str,
            task: str | None,
            *,
            delegation_id: str | None = None,
            chain: list | tuple | None = None,
    ) -> None:
        if self._emit_event(
            RenderEvent.DELEGATION,
            "",
            agent=from_agent,
            metadata={
                "to": to_agent,
                "task": task,
                "delegation_id": delegation_id,
                "chain": list(chain or []),
            },
        ):
            return
        if self._renderer is not None:
            self._renderer.show_delegation(
                from_agent, to_agent, task=task, delegation_id=delegation_id, chain=chain
            )

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
        route = RoutingDecision.coerce(self._parse_routing(user))
        first_agent = route.agent
        message = route.message
        if first_agent is None:
            return
        if not message or not message.strip():
            self._show_warning(MSG_EMPTY_INPUT.format(first_agent))
            return

        if not route.explicit and self._agent_pool is not None:
            reserved_agent = self._agent_pool.take_primary()
            if reserved_agent is not None:
                first_agent = reserved_agent

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
        response, _, _, extend, _ = self._parse_response(response)

        if self._is_cancelled():
            self._handle_cancelled()
            return

        if response is None:
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
                self._notify_failover(failed_agent, fallback_agent)
                fallback_response = self._delegate(
                    fallback_agent,
                    is_first_speaker=True,
                    primary=False,
                    protocol_mode="standard",
                    **prompt_binding,
                )
                fallback_response, _, _, extend, _ = self._parse_response(
                    fallback_response
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                if fallback_response is None:
                    failed_agent = fallback_agent
                    continue
                first_agent = fallback_agent
                self._set_summary_agent_preference(first_agent)
                response = fallback_response
                break

            if response is None:
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                self._show_warning("Nenhum agente disponível respondeu.")
                return

        other_agents = [agent for agent in self._agent_pool.agents if agent != first_agent]
        self._dispatch_services.print_response(first_agent, response)
        if response is not None:
            self._session_services.persist_message(first_agent, response)

        self._process_standard_flow(
            first_agent,
            route.explicit,
            extend,
            other_agents,
            request_override=message,
            history_snapshot=None,
        )

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
                response, _, _, _, _ = self._parse_response(response)
                self._dispatch_services.print_response(agent, response)
                if response is not None:
                    self._session_services.persist_message(agent, response)
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
        self._set_parallel_toolbar_state(
            active=0,
            queued=0,
            capacity=parallel_slots,
            active_agents=(),
        )
