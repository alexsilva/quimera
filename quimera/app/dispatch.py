"""Componentes de `quimera.app.dispatch`."""
import time
from contextlib import nullcontext
from quimera.runtime.errors import (
    ToolError,
    ToolValidationError,
    ToolEnvironmentError,
    ToolLogicError,
    ToolRateLimitError,
)
from ..runtime.tool_hops import get_max_tool_hops
from ..runtime.parser import strip_tool_block
from .config import logger


def _coerce_tool_error(error):
    """Normaliza strings cruas em ToolError quando houver heurística compatível."""
    if not error or isinstance(error, ToolError):
        return error

    error_msg = str(error)
    lowered = error_msg.lower()
    if "validação" in lowered or "campo" in lowered or "formato" in lowered:
        return ToolValidationError(error_msg)
    if "arquivo" in lowered or "permissão" in lowered or "não encontrado" in lowered:
        return ToolEnvironmentError(error_msg)
    if "regra" in lowered or "lógica" in lowered or "contradiz" in lowered:
        return ToolLogicError(error_msg)
    if "rate limit" in lowered or "throttling" in lowered:
        return ToolRateLimitError(error_msg)
    return error


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
        task_services = app.task_services
        current_response = response
        plugin = app.get_agent_plugin(agent)
        max_tool_hops = get_max_tool_hops(getattr(plugin, "tool_use_reliability", "medium"))
        tool_history = []

        for _ in range(max_tool_hops):
            if not current_response:
                return current_response

            raw_response, tool_result = app.tool_executor.maybe_execute_from_response(current_response)

            if tool_result is None:
                return current_response

            is_invalid = bool(getattr(tool_result, "error", None)) and "Sem política para a ferramenta" in str(tool_result.error)
            ok = bool(getattr(tool_result, "ok", False))
            if tool_result.error:
                tool_result.error = _coerce_tool_error(tool_result.error)
            session_metrics = getattr(app, "session_metrics", None)
            if session_metrics is not None:
                session_metrics.record_tool_event(app, agent, ok=ok, is_invalid=is_invalid)

            tool_payload = task_services.truncate_payload(tool_result.to_model_payload())
            tool_history.append(
                f"Sua resposta anterior:\n{current_response.strip()}\n\n"
                f"Resultado da ferramenta:\n{tool_payload}"
            )

            visible_text = strip_tool_block(raw_response or "")
            if visible_text:
                if show_output:
                    app.print_response(agent, visible_text)
                if persist_history:
                    app.session_services.persist_message(agent, visible_text)

            followup_handoff = (
                "Histórico de ferramentas desta rodada:\n\n"
                + "\n\n---\n\n".join(tool_history)
            )

            if hasattr(app, "_call_agent"):
                current_response = app._call_agent(
                    agent,
                    handoff=followup_handoff,
                    primary=False,
                    protocol_mode="tool_loop",
                    silent=silent,
                )
            else:
                current_response = self.call_agent_low_level(
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

        max_retries = getattr(app, "MAX_RETRIES", 2)
        retry_backoff = getattr(app, "RETRY_BACKOFF_SECONDS", 1)
        for attempt in range(1, max_retries + 1):
            if agent_client:
                agent_client._user_cancelled = False
            try:
                response = self.call_agent_low_level(
                    agent,
                    silent=silent,
                    show_output=show_output,
                    **dispatch_options,
                )
                if response is None:
                    if agent_client and agent_client._user_cancelled:
                        logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < max_retries:
                        if agent_client and getattr(agent_client, 'rate_limit_detected', False):
                            backoff = getattr(app, 'RATE_LIMIT_BACKOFF_SECONDS', 30)
                            logger.warning("[DISPATCH] rate limit for agent=%s, waiting %ds before retry", agent, backoff)
                        else:
                            backoff = retry_backoff * attempt
                            logger.warning("[DISPATCH] retry %d/%d for agent=%s", attempt, max_retries, agent)
                        time.sleep(backoff)
                        continue
                    app.record_failure(agent)
                    return None

                result = self.resolve_agent_response(
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
                    if attempt < max_retries:
                        if agent_client and getattr(agent_client, 'rate_limit_detected', False):
                            backoff = getattr(app, 'RATE_LIMIT_BACKOFF_SECONDS', 30)
                            logger.warning("[DISPATCH] rate limit for agent=%s, waiting %ds before retry", agent, backoff)
                        else:
                            backoff = retry_backoff * attempt
                            logger.warning(
                                "[DISPATCH] retry %d/%d for agent=%s (resolve failed)",
                                attempt,
                                max_retries,
                                agent,
                            )
                        time.sleep(backoff)
                        continue
                    app.record_failure(agent)
                return result
            except Exception as exc:
                if agent_client and agent_client._user_cancelled:
                    logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                    return None
                last_error = exc
                if attempt < max_retries:
                    logger.warning(
                        "[DISPATCH] retry %d/%d for agent=%s after exception: %s",
                        attempt,
                        max_retries,
                        agent,
                        exc,
                    )
                    time.sleep(retry_backoff * attempt)
                    continue
                app.record_failure(agent)
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
            show_output=True,
            from_agent=None,
    ):
        """Monta o prompt final e executa a chamada ao backend do agente."""
        app = self.app
        counter_lock = getattr(app, "_counter_lock", None)
        with (counter_lock if counter_lock is not None else nullcontext()):
            app.session_call_index += 1
            call_index_snapshot = app.session_call_index
        start = time.time()
        history = app.history
        app.task_services.refresh_task_shared_state()

        plugin = app.get_agent_plugin(agent)
        driver = plugin.effective_driver() if plugin else "cli"
        stream_state = {"started": False}

        def _on_text_chunk(chunk):
            if silent or not show_output or not chunk:
                return
            output_lock = getattr(app, "_output_lock", None)
            if not stream_state["started"]:
                with (output_lock if output_lock is not None else nullcontext()):
                    if hasattr(app, "_clear_user_prompt_line_if_needed"):
                        app._clear_user_prompt_line_if_needed()
                    app.renderer.start_message_stream(agent)
                    stream_state["started"] = True
            app.renderer.update_message_stream(agent, chunk)

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
                skip_tool_prompt=True,
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
                skip_tool_prompt=True,
            )

        result = app.agent_client.call(agent, prompt, silent=silent, on_text_chunk=_on_text_chunk)
        if stream_state["started"]:
            output_lock = getattr(app, "_output_lock", None)
            with (output_lock if output_lock is not None else nullcontext()):
                if result is not None:
                    app.renderer.finish_message_stream(agent, result)
                else:
                    app.renderer.abort_message_stream(agent)
                if hasattr(app, "_redisplay_user_prompt_if_needed"):
                    app._redisplay_user_prompt_if_needed(clear_first=False)
        elapsed = time.time() - start
        if hasattr(app, "session_state") and app.session_state:
            with (counter_lock if counter_lock is not None else nullcontext()):
                try:
                    app.session_state["handoffs_sent"] += 1
                    app.session_state["total_latency"] += elapsed
                    if result:
                        app.session_state["handoffs_succeeded"] += 1
                    else:
                        app.session_state["handoffs_failed"] += 1
                except KeyError:
                    pass
            session_metrics = getattr(app, "session_metrics", None)
            if session_metrics is not None:
                session_metrics.record_agent_metric(app, agent, "succeeded" if result else "failed", elapsed)
        logger.info("[DISPATCH] agent=%s latency=%.2fs result=%s", agent, elapsed, "ok" if result else "none")
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
            if hasattr(app, "_redisplay_user_prompt_if_needed"):
                app._redisplay_user_prompt_if_needed(clear_first=False)
