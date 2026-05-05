"""Orquestra uma rodada de chat multiagente."""
from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..constants import HANDOFF_SYNTHESIS_MSG, MSG_EMPTY_INPUT, USER_ROLE
from .config import logger


class ChatRoundOrchestrator:
    """Executa o fluxo completo de uma rodada de chat."""

    HANDOFF_MAX_HOPS_FACTOR = 2

    def __init__(self, app):
        self.app = app

    def _show_system(self, message: str) -> None:
        """Exibe mensagem de sistema via camada que preserva prompt quando disponível."""
        app = self.app
        show_system_message = getattr(app, "show_system_message", None)
        if callable(show_system_message):
            try:
                show_system_message(message)
                return
            except AttributeError:
                # Stubs de teste podem expor o método sem inicializar system_layer.
                pass
        app.renderer.show_system(message)

    def process(self, user):
        """Implementação real do processamento de mensagens do chat."""
        app = self.app
        dispatch_services = app.dispatch_services
        first_agent, message, explicit = app.parse_routing(user)
        if first_agent is None:
            return
        if not message or not message.strip():
            app.renderer.show_warning(MSG_EMPTY_INPUT.format(first_agent))
            return

        pending_input_for = getattr(app, "_pending_input_for", None)
        if pending_input_for and not explicit:
            first_agent = pending_input_for
        app._pending_input_for = None

        other_agents = [n for n in app.active_agents if n != first_agent]

        app.round_index += 1
        app.summary_agent_preference = first_agent
        app.session_services.persist_message(USER_ROLE, message)

        response = dispatch_services.call_agent(first_agent, is_first_speaker=True, protocol_mode="standard")
        response, route_target, handoff, extend, needs_human_input, _ = app.parse_response(response)

        agent_client = getattr(app, "agent_client", None)
        if agent_client and agent_client._user_cancelled:
            self._show_system("[cancelado] fluxo interrompido.")
            app.turn_manager.reset()
            return

        if response is None and not route_target and not needs_human_input:
            fallback_candidates = [agent for agent in app.active_agents if agent != first_agent]
            failed_agent = first_agent
            for fallback_agent in fallback_candidates:
                logger.info(
                    "[CHAT_FAILOVER] trying %s after %s returned no response",
                    fallback_agent,
                    failed_agent,
                )
                self._show_system(
                    f"[fallback] {failed_agent} não respondeu; {fallback_agent} assumiu"
                )
                fallback_response = dispatch_services.call_agent(
                    fallback_agent,
                    is_first_speaker=True,
                    primary=False,
                    protocol_mode="standard",
                )
                fallback_response, route_target, handoff, extend, needs_human_input, _ = app.parse_response(
                    fallback_response
                )
                if fallback_response is None and not route_target and not needs_human_input:
                    continue
                first_agent = fallback_agent
                app.summary_agent_preference = first_agent
                response = fallback_response
                break

        if needs_human_input:
            if response:
                app.renderer.show_message(first_agent, response)
            app._pending_input_for = first_agent
            self._show_system(f"\nResponda para {first_agent.upper()}:\n")
            return
        dispatch_services.print_response(first_agent, response)
        if response is not None:
            app.session_services.persist_message(first_agent, response)

        if route_target and handoff:
            self._process_handoff(first_agent, route_target, handoff)
        else:
            self._process_standard_flow(first_agent, explicit, extend, other_agents)

        app.session_services.maybe_auto_summarize(preferred_agent=first_agent)

    def _process_handoff(self, first_agent, route_target, handoff):
        app = self.app
        dispatch_services = app.dispatch_services
        current_from = first_agent
        current_target = route_target
        current_handoff = dict(handoff) if isinstance(handoff, dict) else handoff
        final_response = None
        final_target = None
        original_task = handoff.get("task") if isinstance(handoff, dict) else None
        max_hops = max(1, len(getattr(app, "active_agents", []) or []) * self.HANDOFF_MAX_HOPS_FACTOR)
        hops = 0
        failed_target = current_target

        while current_target and current_handoff and hops < max_hops:
            hops += 1
            handoff_id = current_handoff.get("handoff_id", "?") if isinstance(current_handoff, dict) else "?"
            priority = current_handoff.get("priority", "normal") if isinstance(current_handoff, dict) else "normal"
            chain = current_handoff.get("chain", []) if isinstance(current_handoff, dict) else []

            if current_target in chain:
                logger.warning(
                    "[HANDOFF] Circular delegation detected: %s -> %s (chain: %s)",
                    current_from, current_target, chain,
                )
                if hasattr(app, "behavior_metrics") and app.behavior_metrics:
                    app.behavior_metrics.record_handoff_received(current_target, is_circular=True)
                app.renderer.show_warning(
                    f"Delegação circular detectada: {current_from} -> {current_target}. "
                    f"Cadeia: {' -> '.join(chain + [current_target])}"
                )
                break

            app.renderer.show_handoff(
                current_from,
                current_target,
                task=current_handoff["task"],
            )
            logger.info(
                "[HANDOFF] id=%s from=%s to=%s priority=%s chain=%s",
                handoff_id, current_from, current_target, priority, chain,
            )
            outbound_handoff = dict(current_handoff) if isinstance(current_handoff, dict) else current_handoff
            if isinstance(outbound_handoff, dict):
                outbound_handoff["chain"] = chain + [current_from]
            response_handoff = outbound_handoff
            if hasattr(app, "behavior_metrics") and app.behavior_metrics:
                app.behavior_metrics.record_handoff_sent(current_from)
                app.behavior_metrics.record_handoff_received(current_target)

            secondary_response = dispatch_services.call_agent(
                current_target,
                handoff=outbound_handoff,
                handoff_only=True,
                primary=False,
                protocol_mode="handoff",
                from_agent=current_from,
            )
            agent_client = getattr(app, "agent_client", None)
            if agent_client and agent_client._user_cancelled:
                self._show_system("[cancelado] fluxo interrompido.")
                app.turn_manager.reset()
                return
            expected_ack = current_handoff.get("handoff_id") if isinstance(current_handoff, dict) else None
            secondary_response, next_target, next_handoff, _, _, ack_id = app.parse_response(secondary_response)
            if expected_ack and ack_id and ack_id != expected_ack:
                logger.warning(
                    "[ACK] mismatch: expected=%s, received=%s from agent=%s",
                    expected_ack, ack_id, current_target,
                )
            dispatch_services.print_response(current_target, secondary_response)
            if secondary_response is not None:
                app.session_services.persist_message(current_target, secondary_response)

            if not secondary_response:
                fallback_response = None
                fallback_target = None
                fallback_next_target = None
                fallback_next_handoff = None
                fallback_candidates = [
                    agent for agent in app.active_agents
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
                    fallback_response = dispatch_services.call_agent(
                        fallback_agent,
                        handoff=fallback_handoff,
                        handoff_only=True,
                        primary=False,
                        protocol_mode="handoff",
                        from_agent=current_from,
                    )
                    agent_client = getattr(app, "agent_client", None)
                    if agent_client and agent_client._user_cancelled:
                        self._show_system("[cancelado] fluxo interrompido.")
                        app.turn_manager.reset()
                        return
                    fallback_response, fallback_next_target, fallback_next_handoff, _, _, ack_id = app.parse_response(
                        fallback_response
                    )
                    dispatch_services.print_response(fallback_agent, fallback_response)
                    if fallback_response is not None:
                        app.session_services.persist_message(fallback_agent, fallback_response)
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
                current_from = current_target
                current_target = next_target
                current_handoff = next_handoff
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
            synthesis_handoff = HANDOFF_SYNTHESIS_MSG.format(
                agent=final_target.upper(),
                task=original_task or current_handoff["task"],
                response=final_response,
            )
            if hasattr(app, "behavior_metrics") and app.behavior_metrics:
                app.behavior_metrics.record_synthesis(first_agent)
            final_response = dispatch_services.call_agent(
                first_agent,
                handoff=synthesis_handoff,
                primary=False,
                protocol_mode="handoff",
            )
            agent_client = getattr(app, "agent_client", None)
            if agent_client and agent_client._user_cancelled:
                self._show_system("[cancelado] fluxo interrompido.")
                app.turn_manager.reset()
                return
            final_response, _, _, _, _, _ = app.parse_response(final_response)
            dispatch_services.print_response(first_agent, final_response)
            if final_response is not None:
                app.session_services.persist_message(first_agent, final_response)
        else:
            logger.warning(
                "[HANDOFF] id=%s failed: secondary agent %s returned no response",
                handoff.get("handoff_id", "?") if isinstance(handoff, dict) else "?",
                failed_target,
            )
            self._show_system(
                f"[handoff] {failed_target} não respondeu — delegação falhou"
            )

    def _process_standard_flow(self, first_agent, explicit, extend, other_agents):
        app = self.app
        dispatch_services = app.dispatch_services
        protocol_mode = "extended" if extend else "standard"
        if explicit or not extend:
            remaining = []
        else:
            remaining = [other_agents[0], first_agent, other_agents[0]] if other_agents else []

        next_handoff = None
        if app.threads > 1 and len(remaining) > 1:
            staging_root = Path(
                tempfile.gettempdir()
            ) / "quimera-staging" / f"{app.session_state['session_id']}-round{app.round_index}"
            staging_root.mkdir(parents=True, exist_ok=True)
            logger.info("parallel mode: %d threads, staging=%s", app.threads, staging_root)
            native_tool_agents = [
                a for a in remaining
                if getattr(app.get_agent_plugin(a), "output_format", None) == "stream-json"
            ]
            if native_tool_agents:
                logger.warning(
                    "[parallel] agentes com tools nativas não usam staging: %s — "
                    "escritas de arquivo vão direto ao disco e podem conflitar",
                    native_tool_agents,
                )
            try:
                with ThreadPoolExecutor(max_workers=app.threads) as executor:
                    agent_handoff_pairs = [(agent, None, staging_root, i) for i, agent in enumerate(remaining)]
                    futures = [
                        executor.submit(
                            app.task_services.call_agent_for_parallel,
                            agent,
                            handoff,
                            protocol_mode,
                            staging_dir,
                            idx,
                        )
                        for agent, handoff, staging_dir, idx in agent_handoff_pairs
                    ]
                    results = [future.result() for future in futures]
                app._merge_staging_to_workspace(staging_root)
                agent_client = getattr(app, "agent_client", None)
                if agent_client and agent_client._user_cancelled:
                    self._show_system("[cancelado] fluxo interrompido.")
                    app.turn_manager.reset()
                    return
                needs_input_any = False
                for item in results:
                    agent, response, route_target, handoff, extend, needs_input = item
                    dispatch_services.print_response(agent, response)
                    if response is not None:
                        app.session_services.persist_message(agent, response)
                    needs_input_any = needs_input or needs_input_any
                if needs_input_any:
                    needing = next((agent_data for agent_data in results if agent_data[-1]), None)
                    if needing:
                        current_agent = needing[0]
                        app._pending_input_for = current_agent
                        self._show_system(f"\nResponda para {current_agent.upper()}:\n")
            finally:
                if staging_root.exists():
                    shutil.rmtree(staging_root)
                    logger.info("staging cleanup: %s removed", staging_root)
            return

        for index, agent in enumerate(remaining):
            response = dispatch_services.call_agent(agent, handoff=next_handoff, primary=False, protocol_mode=protocol_mode)
            agent_client = getattr(app, "agent_client", None)
            if agent_client and agent_client._user_cancelled:
                self._show_system("[cancelado] fluxo interrompido.")
                app.turn_manager.reset()
                return
            next_handoff = None
            response, route_target, handoff, _, needs_human_input, _ = app.parse_response(response)
            dispatch_services.print_response(agent, response)
            if response is not None:
                app.session_services.persist_message(agent, response)
            if needs_human_input:
                app._pending_input_for = agent
                self._show_system(f"\nResponda para {agent.upper()}:\n")
                break
            if route_target and index + 1 < len(remaining):
                remaining[index + 1] = route_target
            if route_target:
                next_handoff = handoff
