"""Orquestra uma rodada de chat multiagente."""
from __future__ import annotations

import inspect
import threading

from ..constants import HANDOFF_SYNTHESIS_MSG, MSG_EMPTY_INPUT, USER_ROLE
from ..prompt_kinds import PromptKind
from .config import logger
from .render_event import RenderEvent
from ..domain.session_state import SessionState

class ChatRoundOrchestrator:
    """Executa o fluxo completo de uma rodada de chat."""

    HANDOFF_MAX_HOPS_FACTOR = 2

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
        get_agent_plugin=None,
        behavior_metrics=None,
        threads=1,
        session_state=None,
        show_system_message=None,
        renderer=None,
        ui_queue=None,
        merge_staging_to_workspace=None,
        generate_handoff_id=None,
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
            get_agent_plugin = getattr(app, "get_agent_plugin", get_agent_plugin)
            behavior_metrics = getattr(app, "behavior_metrics", behavior_metrics)
            threads = getattr(app, "threads", threads)
            session_state = getattr(app, "_chat_state", getattr(app, "session_state", session_state))
            show_system_message = getattr(app, "show_system_message", show_system_message)
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
            merge_staging_to_workspace = merge_staging_to_workspace or getattr(
                app, "_merge_staging_to_workspace", None
            )
            generate_handoff_id = generate_handoff_id or getattr(
                app, "_generate_handoff_id", None
            )

        self._dispatch_services = dispatch_services
        self._parse_routing = parse_routing
        self._agent_pool = agent_pool
        self._session_services = session_services
        self._parse_response = parse_response
        self._agent_client = agent_client
        self._turn_manager = turn_manager
        self._task_services = task_services
        self._get_agent_plugin = get_agent_plugin
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
        self._generate_handoff_id = generate_handoff_id or (lambda task, target: f"gen-{target}")
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

    def _call_agent(self, agent: str, **kwargs):
        if self._threads > 1 and "max_retries" not in kwargs:
            # Em modo threaded cada prompt deve executar uma vez por agente;
            # retries automáticos causam efeito de loop/cascata no chat.
            kwargs["max_retries"] = 1
        call_agent = self._dispatch_services.call_agent
        filtered_kwargs = self._filter_supported_kwargs(call_agent, kwargs)
        return call_agent(agent, **filtered_kwargs)

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

    def _show_handoff(self, from_agent: str, to_agent: str, task: str | None) -> None:
        if self._emit_event(
            RenderEvent.HANDOFF,
            "",
            agent=from_agent,
            metadata={"to": to_agent, "task": task},
        ):
            return
        if self._renderer is not None:
            show_handoff = getattr(self._renderer, "show_handoff", None)
            if callable(show_handoff):
                show_handoff(from_agent, to_agent, task=task)

    @staticmethod
    def _build_synthesis_payload(responses: list[tuple[str, str]]) -> tuple[str, str]:
        if len(responses) == 1:
            agent, response = responses[0]
            return agent.upper(), response

        parts = []
        for agent, response in responses:
            parts.append(f"{agent.upper()}:\n{response}")
        return "AGENTES DELEGADOS", "\n\n".join(parts)

    @staticmethod
    def _copy_pending_handoffs(handoff: dict | None) -> list[dict]:
        if not isinstance(handoff, dict):
            return []
        pending = handoff.get("_pending_handoffs", [])
        if not isinstance(pending, list):
            return []
        return [dict(item) for item in pending if isinstance(item, dict)]

    def _is_cancelled(self) -> bool:
        return bool(self._agent_client and getattr(self._agent_client, '_user_cancelled', False))

    def _handle_cancelled(self) -> None:
        if bool(getattr(self._cancel_notice_tls, "shown", False)):
            return
        self._cancel_notice_tls.shown = True
        self._show_system("[cancelado] fluxo interrompido.")
        if self._turn_manager is not None:
            self._turn_manager.reset()

    def process(self, user):
        """Implementação real do processamento de mensagens do chat."""
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

        other_agents = [n for n in self._agent_pool.agents if n != first_agent]

        self._set_round_index(self._get_round_index() + 1)
        self._set_summary_agent_preference(first_agent)
        history_snapshot = self._persist_user_message(message)
        prompt_binding = {"request_override": message}
        if history_snapshot:
            prompt_binding["history_snapshot"] = history_snapshot

        response = self._call_agent(
            first_agent,
            is_first_speaker=True,
            protocol_mode="standard",
            **prompt_binding,
        )
        response, route_target, handoff, extend, needs_human_input, _ = self._parse_response(response)

        if self._is_cancelled():
            self._handle_cancelled()
            return

        if response is None and not route_target and not needs_human_input:
            if self._is_cancelled():
                self._handle_cancelled()
                return

            fallback_candidates = [agent for agent in self._agent_pool.agents if agent != first_agent]
            failed_agent = first_agent
            for fallback_agent in fallback_candidates:
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                logger.info(
                    "[CHAT_FAILOVER] trying %s after %s returned no response",
                    fallback_agent,
                    failed_agent,
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                self._show_system(
                    f"[fallback] {failed_agent} não respondeu; {fallback_agent} assumiu"
                )
                fallback_response = self._call_agent(
                    fallback_agent,
                    is_first_speaker=True,
                    primary=False,
                    protocol_mode="standard",
                    **prompt_binding,
                )
                fallback_response, route_target, handoff, extend, needs_human_input, _ = self._parse_response(
                    fallback_response
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                if fallback_response is None and not route_target and not needs_human_input:
                    failed_agent = fallback_agent
                    continue
                first_agent = fallback_agent
                self._set_summary_agent_preference(first_agent)
                response = fallback_response
                break

            if response is None and not route_target and not needs_human_input:
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                self._show_warning("Nenhum agente disponível respondeu.")
                return

        if needs_human_input:
            if response:
                self._show_agent_message(first_agent, response)
            self._set_pending_input_for(first_agent)
            self._show_system(f"Responda para {first_agent.upper()}:")
            return
        self._dispatch_services.print_response(first_agent, response)
        if response is not None:
            self._session_services.persist_message(first_agent, response)

        if route_target and route_target == first_agent:
            logger.warning(
                "[HANDOFF] %s attempted to handoff to itself — ignored",
                first_agent,
            )
            route_target = None
            handoff = None

        connected_agents = self._agent_pool.agents
        if route_target and route_target not in connected_agents:
            logger.warning(
                "[HANDOFF] %s attempted to handoff to unknown agent %r — ignored (active: %s)",
                first_agent,
                route_target,
                connected_agents,
            )
            route_target = None
            handoff = None

        if route_target and handoff:
            self._process_handoff(
                first_agent,
                route_target,
                handoff,
                request_override=message,
                history_snapshot=history_snapshot if history_snapshot else None,
            )
        else:
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

    def _process_handoff(
        self,
        first_agent,
        route_target,
        handoff,
        *,
        request_override: str | None = None,
        history_snapshot: list | None = None,
    ):
        current_from = first_agent
        current_target = route_target
        current_handoff = dict(handoff) if isinstance(handoff, dict) else handoff
        final_response = None
        final_target = None
        original_task = handoff.get("task") if isinstance(handoff, dict) else None
        collected_responses: list[tuple[str, str]] = []
        max_hops = max(1, len(self._agent_pool.agents) * self.HANDOFF_MAX_HOPS_FACTOR)
        hops = 0
        failed_target = current_target

        call_prompt_binding = {}
        if request_override:
            call_prompt_binding["request_override"] = request_override
        if history_snapshot:
            call_prompt_binding["history_snapshot"] = history_snapshot

        while current_target and current_handoff and hops < max_hops:
            hops += 1
            handoff_id = current_handoff.get("handoff_id", "?") if isinstance(current_handoff, dict) else "?"
            priority = current_handoff.get("priority", "normal") if isinstance(current_handoff, dict) else "normal"
            chain = current_handoff.get("chain", []) if isinstance(current_handoff, dict) else []

            if current_target == current_from:
                logger.warning(
                    "[HANDOFF] %s attempted to handoff to itself in chain — ignored",
                    current_target,
                )
                self._show_warning(f"{current_target} tentou delegar para si mesmo — ignorado")
                break

            if current_target in chain:
                logger.warning(
                    "[HANDOFF] Circular delegation detected: %s -> %s (chain: %s)",
                    current_from, current_target, chain,
                )
                if self._behavior_metrics is not None:
                    self._behavior_metrics.record_handoff_received(current_target, is_circular=True)
                self._show_warning(
                    f"Delegação circular detectada: {current_from} -> {current_target}. "
                    f"Cadeia: {' -> '.join(chain + [current_target])}"
                )
                break

            _active = self._agent_pool.agents
            if current_target not in _active:
                logger.warning(
                    "[HANDOFF] %s attempted to handoff to unknown agent %r in chain — ignored (active: %s)",
                    current_from,
                    current_target,
                    _active,
                )
                self._show_warning(
                    f"Handoff ignorado: agente '{current_target}' não está conectado."
                )
                break

            self._show_handoff(
                current_from,
                current_target,
                current_handoff.get("task") if isinstance(current_handoff, dict) else None,
            )
            logger.info(
                "[HANDOFF] id=%s from=%s to=%s priority=%s chain=%s",
                handoff_id, current_from, current_target, priority, chain,
            )
            outbound_handoff = dict(current_handoff) if isinstance(current_handoff, dict) else current_handoff
            if isinstance(outbound_handoff, dict):
                outbound_handoff["chain"] = chain + [current_from]
            response_handoff = outbound_handoff
            if self._behavior_metrics is not None:
                self._behavior_metrics.record_handoff_sent(current_from)
                self._behavior_metrics.record_handoff_received(current_target)

            secondary_response = self._call_agent(
                current_target,
                handoff=outbound_handoff,
                handoff_only=True,
                primary=False,
                protocol_mode="handoff",
                from_agent=current_from,
                prompt_kind=PromptKind.TASK_EXECUTOR,
                **call_prompt_binding,
            )
            if self._is_cancelled():
                self._handle_cancelled()
                return
            expected_ack = current_handoff.get("handoff_id") if isinstance(current_handoff, dict) else None
            secondary_response, next_target, next_handoff, _, _, ack_id = self._parse_response(secondary_response)
            if expected_ack and ack_id and ack_id != expected_ack:
                logger.warning(
                    "[ACK] mismatch: expected=%s, received=%s from agent=%s",
                    expected_ack, ack_id, current_target,
                )
            self._dispatch_services.print_response(current_target, secondary_response)
            if secondary_response is not None:
                self._session_services.persist_message(current_target, secondary_response)
                collected_responses.append((current_target, secondary_response))

            if not secondary_response and not (next_target and next_handoff):
                fallback_response = None
                fallback_target = None
                fallback_next_target = None
                fallback_next_handoff = None
                fallback_candidates = [
                    agent for agent in self._agent_pool.agents
                    if agent != first_agent and agent != current_from and agent != current_target and agent not in chain
                ]
                for fallback_agent in fallback_candidates:
                    logger.info(
                        "[HANDOFF] id=%s fallback: trying %s after %s failed",
                        handoff_id, fallback_agent, current_target,
                    )
                    self._show_system(
                        f"[handoff] tentando fallback: {fallback_agent} (após {current_target} falhar)"
                    )
                    fallback_handoff = dict(current_handoff) if isinstance(current_handoff, dict) else current_handoff
                    if isinstance(fallback_handoff, dict):
                        fallback_chain = []
                        if isinstance(outbound_handoff, dict):
                            fallback_chain.extend(outbound_handoff.get("chain", []))
                        fallback_chain.append(current_target)
                        fallback_handoff["chain"] = fallback_chain
                    fallback_response = self._call_agent(
                        fallback_agent,
                        handoff=fallback_handoff,
                        handoff_only=True,
                        primary=False,
                        protocol_mode="handoff",
                        from_agent=current_from,
                        prompt_kind=PromptKind.TASK_EXECUTOR,
                        **call_prompt_binding,
                    )
                    if self._is_cancelled():
                        self._handle_cancelled()
                        return
                    fallback_response, fallback_next_target, fallback_next_handoff, _, _, ack_id = self._parse_response(
                        fallback_response
                    )
                    self._dispatch_services.print_response(fallback_agent, fallback_response)
                    if fallback_response is not None:
                        self._session_services.persist_message(fallback_agent, fallback_response)
                        collected_responses.append((fallback_agent, fallback_response))
                    if fallback_response or (fallback_next_target and fallback_next_handoff):
                        fallback_target = fallback_agent
                        break

                if fallback_target is None:
                    failed_target = current_target
                    break

                secondary_response = fallback_response
                current_target = fallback_target
                next_target = fallback_next_target
                next_handoff = fallback_next_handoff
                response_handoff = fallback_handoff

            failed_target = current_target

            if next_target and next_handoff:
                if isinstance(next_handoff, dict):
                    propagated_chain = []
                    if isinstance(response_handoff, dict):
                        propagated_chain.extend(response_handoff.get("chain", []))
                    propagated_chain.append(current_target)
                    existing_chain = next_handoff.get("chain", [])
                    for chain_agent in existing_chain:
                        if chain_agent not in propagated_chain:
                            propagated_chain.append(chain_agent)
                    next_handoff["chain"] = propagated_chain
                    pending = self._copy_pending_handoffs(current_handoff)
                    if pending:
                        next_handoff["_pending_handoffs"] = pending
                current_from = current_target
                current_target = next_target
                current_handoff = next_handoff
                continue

            pending_handoffs = self._copy_pending_handoffs(current_handoff)
            if pending_handoffs:
                next_item = pending_handoffs[0]
                next_target = next_item["route"]
                current_handoff = {
                    "task": next_item["content"],
                    "chain": [],
                }
                if isinstance(next_item.get("metadata"), dict):
                    current_handoff.update(next_item["metadata"])
                current_handoff["handoff_id"] = next_item.get("handoff_id") or self._generate_handoff_id(
                    next_item["content"],
                    next_target,
                )
                current_handoff["priority"] = current_handoff.get("priority", "normal")
                if len(pending_handoffs) > 1:
                    current_handoff["_pending_handoffs"] = pending_handoffs[1:]
                current_from = first_agent
                current_target = next_target
                logger.info(
                    "[HANDOFF] pending handoff: proceeding to %s after %s",
                    next_target, current_from,
                )
                continue

            if secondary_response:
                final_response = secondary_response
                final_target = current_target
                break

        if not final_response and hops >= max_hops:
            logger.warning(
                "[HANDOFF] id=%s failed: max hops exceeded (%s)",
                handoff.get("handoff_id", "?") if isinstance(handoff, dict) else "?",
                max_hops,
            )
            self._show_system("[handoff] limite de encadeamentos atingido — delegação interrompida")
            return

        if final_response and final_target:
            synthesis_agent, synthesis_response = self._build_synthesis_payload(collected_responses or [(final_target, final_response)])
            synthesis_handoff = HANDOFF_SYNTHESIS_MSG.format(
                agent=synthesis_agent,
                task=original_task or current_handoff["task"],
                response=synthesis_response,
            )
            if self._behavior_metrics is not None:
                self._behavior_metrics.record_synthesis(first_agent)
            final_response = self._call_agent(
                first_agent,
                handoff=synthesis_handoff,
                primary=False,
                protocol_mode="handoff",
                **call_prompt_binding,
            )
            if self._is_cancelled():
                self._handle_cancelled()
                return
            final_response, _, _, _, _, _ = self._parse_response(final_response)
            self._dispatch_services.print_response(first_agent, final_response)
            if final_response is not None:
                self._session_services.persist_message(first_agent, final_response)
        else:
            logger.warning(
                "[HANDOFF] id=%s failed: secondary agent %s returned no response",
                handoff.get("handoff_id", "?") if isinstance(handoff, dict) else "?",
                failed_target,
            )
            self._show_system(
                f"[handoff] {failed_target} não respondeu — delegação falhou"
            )

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
            remaining = [other_agents[0], first_agent, other_agents[0]] if other_agents else []
            next_handoff = None
            for index, agent in enumerate(remaining):
                self._set_parallel_toolbar_state(
                    active=0,
                    queued=max(0, len(remaining) - (index + 1)),
                    capacity=parallel_slots,
                    active_agents=(),
                )
                response = self._call_agent(
                    agent,
                    handoff=next_handoff,
                    primary=False,
                    protocol_mode=protocol_mode,
                    **call_prompt_binding,
                )
                if self._is_cancelled():
                    self._handle_cancelled()
                    return
                next_handoff = None
                response, route_target, handoff, _, needs_human_input, _ = self._parse_response(response)
                self._dispatch_services.print_response(agent, response)
                if response is not None:
                    self._session_services.persist_message(agent, response)
                if needs_human_input:
                    self._set_pending_input_for(agent)
                    self._show_system(f"Responda para {agent.upper()}:")
                    break
                if route_target == agent:
                    logger.warning(
                        "[HANDOFF] %s attempted to handoff to itself in standard flow — ignored",
                        agent,
                    )
                    self._show_warning(f"{agent} tentou delegar para si mesmo — ignorado")
                    route_target = None
                    handoff = None
                if route_target:
                    next_handoff = handoff
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
