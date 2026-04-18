"""Componentes de `quimera.app.dispatch`."""
import time

from .. import plugins
from ..runtime.parser import strip_tool_block
from . import task as app_tasks
from .config import logger


class AppDispatchServices:
    """Agrupa despacho de agentes e persistência de mensagens."""

    def __init__(self, app):
        """Inicializa uma instância de AppDispatchServices."""
        self.app = app

    def resolve_agent_response(
            self,
            agent: str,
            response: str | None,
            silent: bool = False,
            persist_history: bool = True,
            show_output: bool = True,
    ) -> str | None:
        """Resolve respostas com loop de ferramentas até estabilizar a saída."""
        app = self.app
        current_response = response
        max_tool_hops = 16
        tool_history = []

        for _ in range(max_tool_hops):
            if not current_response:
                return current_response

            raw_response, tool_result = app.tool_executor.maybe_execute_from_response(current_response)

            if tool_result is None:
                return current_response

            app._record_tool_event(agent, result=tool_result)

            tool_payload = app_tasks.truncate_payload(tool_result.to_model_payload())
            tool_history.append(
                f"Sua resposta anterior:\n{current_response.strip()}\n\n"
                f"Resultado da ferramenta:\n{tool_payload}"
            )

            visible_text = strip_tool_block(raw_response or "")
            if visible_text:
                if show_output:
                    app.print_response(agent, visible_text)
                if persist_history:
                    app.persist_message(agent, visible_text)

            followup_handoff = (
                "Histórico de ferramentas desta rodada:\n\n"
                + "\n\n---\n\n".join(tool_history)
            )

            current_response = app._call_agent(
                agent,
                handoff=followup_handoff,
                primary=False,
                protocol_mode="tool_loop",
                silent=silent,
            )

        return "Falha: limite de execuções de ferramenta atingido."

    def call_agent(self, agent, **options):
        """Executa despacho com retry e resolução de ferramentas."""
        app = self.app
        dispatch_options = dict(options)
        silent = dispatch_options.pop("silent", False)
        persist_history = dispatch_options.pop("persist_history", True)
        show_output = dispatch_options.pop("show_output", True)
        dispatch_options.pop("quiet", False)
        handoff = dispatch_options.get("handoff")
        handoff_id = handoff.get("handoff_id") if isinstance(handoff, dict) else None
        logger.info(
            "[DISPATCH] sending to agent=%s, handoff_only=%s, handoff_id=%s",
            agent, dispatch_options.get("handoff_only", False), handoff_id,
        )
        last_error = None
        agent_client = getattr(app, "agent_client", None)

        for attempt in range(1, app.MAX_RETRIES + 1):
            if agent_client:
                agent_client._user_cancelled = False
            try:
                response = app._call_agent(agent, silent=silent, **dispatch_options)
                if response is None:
                    if agent_client and agent_client._user_cancelled:
                        logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < app.MAX_RETRIES:
                        logger.warning("[DISPATCH] retry %d/%d for agent=%s", attempt, app.MAX_RETRIES, agent)
                        time.sleep(app.RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    app._record_failure(agent)
                    return None

                result = app.resolve_agent_response(
                    agent,
                    response,
                    silent=silent,
                    persist_history=persist_history,
                    show_output=show_output,
                )
                if result is None:
                    if agent_client and agent_client._user_cancelled:
                        logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < app.MAX_RETRIES:
                        logger.warning(
                            "[DISPATCH] retry %d/%d for agent=%s (resolve failed)",
                            attempt,
                            app.MAX_RETRIES,
                            agent,
                        )
                        time.sleep(app.RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    app._record_failure(agent)
                return result
            except Exception as exc:
                if agent_client and agent_client._user_cancelled:
                    logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                    return None
                last_error = exc
                if attempt < app.MAX_RETRIES:
                    logger.warning(
                        "[DISPATCH] retry %d/%d for agent=%s after exception: %s",
                        attempt,
                        app.MAX_RETRIES,
                        agent,
                        exc,
                    )
                    time.sleep(app.RETRY_BACKOFF_SECONDS * attempt)
                    continue
                app._record_failure(agent)
                raise

        if last_error:
            logger.error("[DISPATCH] all retries exhausted for agent=%s", agent)
        return None

    def call_agent_low_level(
            self,
            agent,
            is_first_speaker=False,
            handoff=None,
            primary=True,
            protocol_mode="standard",
            handoff_only=False,
            silent=False,
            from_agent=None,
    ):
        """Monta o prompt final e executa a chamada ao backend do agente."""
        app = self.app
        with app._counter_lock:
            app.session_call_index += 1
            call_index_snapshot = app.session_call_index
        start = time.time()
        history = [] if handoff_only else app.history
        app._get_task_services().refresh_task_shared_state()

        plugin = plugins.get(agent)
        driver = getattr(plugin, "driver", "cli") if plugin else "cli"
        skip_tool_prompt = isinstance(driver, str) and driver != "cli"
        if app.debug_prompt_metrics:
            prompt, metrics = app.prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                handoff,
                debug=True,
                primary=primary,
                shared_state=app.shared_state,
                handoff_only=handoff_only,
                from_agent=from_agent,
                skip_tool_prompt=skip_tool_prompt,
            )
            app.agent_client.log_prompt_metrics(
                agent,
                metrics,
                session_id=app.session_state["session_id"],
                round_index=app.round_index,
                session_call_index=call_index_snapshot,
                history_window=app.prompt_builder.history_window,
                protocol_mode=protocol_mode,
            )
        else:
            prompt = app.prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                handoff,
                primary=primary,
                shared_state=app.shared_state,
                handoff_only=handoff_only,
                from_agent=from_agent,
                skip_tool_prompt=skip_tool_prompt,
            )

        result = app.agent_client.call(agent, prompt, silent=silent)
        elapsed = time.time() - start
        if hasattr(app, "session_state") and app.session_state:
            with app._counter_lock:
                try:
                    app.session_state["handoffs_sent"] += 1
                    app.session_state["total_latency"] += elapsed
                    if result:
                        app.session_state["handoffs_succeeded"] += 1
                    else:
                        app.session_state["handoffs_failed"] += 1
                except KeyError:
                    pass
            app._record_agent_metric(agent, "succeeded" if result else "failed", elapsed)
        logger.info("[DISPATCH] agent=%s latency=%.2fs result=%s", agent, elapsed, "ok" if result else "none")
        return result

    def print_response(self, agent, response):
        """Exibe saída do agente preservando o prompt não bloqueante."""
        app = self.app
        with app._output_lock:
            app._clear_user_prompt_line_if_needed()
            if response is not None:
                app.renderer.show_message(agent, response)
            else:
                app.renderer.show_no_response(agent)
            app._redisplay_user_prompt_if_needed(clear_first=False)

    def persist_message(self, role, content):
        """Persiste mensagem no histórico, log e snapshot."""
        app = self.app
        with app._lock:
            app.history.append({"role": role, "content": content})
            app.storage.append_log(role, content)
            app.storage.save_history(app.history, shared_state=app.shared_state)
            app._get_session_metrics().update_persisted_message_metrics(app, role, content)
