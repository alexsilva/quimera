"""Orquestra uma rodada de chat multiagente."""
from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import plugins
from ..constants import HANDOFF_SYNTHESIS_MSG, MSG_EMPTY_INPUT, USER_ROLE
from .config import logger


class ChatRoundOrchestrator:
    """Executa o fluxo completo de uma rodada de chat."""

    def __init__(self, app):
        self.app = app

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
            app.renderer.show_system("[cancelado] fluxo interrompido.")
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
                app.renderer.show_system(
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
            app.renderer.show_system(f"\nResponda para {first_agent.upper()}:\n")
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
        handoff_id = handoff.get("handoff_id", "?")
        priority = handoff.get("priority", "normal")
        chain = handoff.get("chain", []) if isinstance(handoff, dict) else []
        if route_target in chain:
            logger.warning(
                "[HANDOFF] Circular delegation detected: %s -> %s (chain: %s)",
                first_agent, route_target, chain,
            )
            if hasattr(app, "behavior_metrics") and app.behavior_metrics:
                app.behavior_metrics.record_handoff_received(route_target, is_circular=True)
            app.renderer.show_warning(
                f"Delegação circular detectada: {first_agent} -> {route_target}. "
                f"Cadeia: {' -> '.join(chain + [route_target])}"
            )
            return

        app.renderer.show_handoff(
            first_agent,
            route_target,
            task=handoff["task"],
        )
        logger.info(
            "[HANDOFF] id=%s from=%s to=%s priority=%s chain=%s",
            handoff_id, first_agent, route_target, priority, chain,
        )
        if isinstance(handoff, dict):
            handoff["chain"] = chain + [first_agent]
        if hasattr(app, "behavior_metrics") and app.behavior_metrics:
            app.behavior_metrics.record_handoff_sent(first_agent)
            app.behavior_metrics.record_handoff_received(route_target)
        secondary_response = dispatch_services.call_agent(
            route_target,
            handoff=handoff,
            handoff_only=True,
            primary=False,
            protocol_mode="handoff",
            from_agent=first_agent,
        )
        agent_client = getattr(app, "agent_client", None)
        if agent_client and agent_client._user_cancelled:
            app.renderer.show_system("[cancelado] fluxo interrompido.")
            app.turn_manager.reset()
            return
        expected_ack = handoff.get("handoff_id")
        secondary_response, _, _, _, _, ack_id = app.parse_response(secondary_response)
        if expected_ack and ack_id and ack_id != expected_ack:
            logger.warning(
                "[ACK] mismatch: expected=%s, received=%s from agent=%s",
                expected_ack, ack_id, route_target,
            )
        dispatch_services.print_response(route_target, secondary_response)
        if secondary_response is not None:
            app.session_services.persist_message(route_target, secondary_response)

        if not secondary_response:
            fallback_candidates = [
                a for a in app.active_agents
                if a != first_agent and a != route_target and a not in chain
            ]
            for fallback_agent in fallback_candidates:
                logger.info(
                    "[HANDOFF] id=%s fallback: trying %s after %s failed",
                    handoff_id, fallback_agent, route_target,
                )
                app.renderer.show_system(
                    f"[handoff] tentando fallback: {fallback_agent} (após {route_target} falhar)"
                )
                fallback_handoff = dict(handoff) if isinstance(handoff, dict) else handoff
                if isinstance(fallback_handoff, dict):
                    fallback_handoff["chain"] = handoff.get("chain", []) + [route_target]
                secondary_response = dispatch_services.call_agent(
                    fallback_agent,
                    handoff=fallback_handoff,
                    handoff_only=True,
                    primary=False,
                    protocol_mode="handoff",
                    from_agent=first_agent,
                )
                agent_client = getattr(app, "agent_client", None)
                if agent_client and agent_client._user_cancelled:
                    app.renderer.show_system("[cancelado] fluxo interrompido.")
                    app.turn_manager.reset()
                    return
                secondary_response, _, _, _, _, ack_id = app.parse_response(secondary_response)
                if secondary_response:
                    route_target = fallback_agent
                    dispatch_services.print_response(fallback_agent, secondary_response)
                    app.session_services.persist_message(fallback_agent, secondary_response)
                    break

        if secondary_response:
            synthesis_handoff = HANDOFF_SYNTHESIS_MSG.format(
                agent=route_target.upper(),
                task=handoff["task"],
                response=secondary_response,
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
                app.renderer.show_system("[cancelado] fluxo interrompido.")
                app.turn_manager.reset()
                return
            final_response, _, _, _, _, _ = app.parse_response(final_response)
            dispatch_services.print_response(first_agent, final_response)
            if final_response is not None:
                app.session_services.persist_message(first_agent, final_response)
        else:
            logger.warning(
                "[HANDOFF] id=%s failed: secondary agent %s returned no response",
                handoff_id, route_target,
            )
            app.renderer.show_system(
                f"[handoff] {route_target} não respondeu — delegação falhou"
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
                if getattr(plugins.get(a), "output_format", None) == "stream-json"
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
                            app._call_agent_for_parallel,
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
                    app.renderer.show_system("[cancelado] fluxo interrompido.")
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
                        app.renderer.show_system(f"\nResponda para {current_agent.upper()}:\n")
            finally:
                if staging_root.exists():
                    shutil.rmtree(staging_root)
                    logger.info("staging cleanup: %s removed", staging_root)
            return

        for index, agent in enumerate(remaining):
            response = dispatch_services.call_agent(agent, handoff=next_handoff, primary=False, protocol_mode=protocol_mode)
            agent_client = getattr(app, "agent_client", None)
            if agent_client and agent_client._user_cancelled:
                app.renderer.show_system("[cancelado] fluxo interrompido.")
                app.turn_manager.reset()
                return
            next_handoff = None
            response, route_target, handoff, _, needs_human_input, _ = app.parse_response(response)
            dispatch_services.print_response(agent, response)
            if response is not None:
                app.session_services.persist_message(agent, response)
            if needs_human_input:
                app._pending_input_for = agent
                app.renderer.show_system(f"\nResponda para {agent.upper()}:\n")
                break
            if route_target and index + 1 < len(remaining):
                remaining[index + 1] = route_target
            if route_target:
                next_handoff = handoff
