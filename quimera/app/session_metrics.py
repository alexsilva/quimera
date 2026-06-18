"""Componentes de `quimera.app.session_metrics`."""
from difflib import SequenceMatcher


class SessionMetricsService:
    """Centraliza heurísticas de qualidade de resposta e métricas por agente."""

    _POLICY_ERROR_MARKERS = (
        "sem política para a ferramenta",
        "bloqueada pelo modo de execução",
        "comando bloqueado",
        "comando inválido",
        "comando fora da allowlist",
        "path fora da workspace",
    )

    @classmethod
    def classify_tool_event_result(cls, result) -> tuple[bool, bool, str]:
        """Normaliza o resultado de tool call para métricas de sessão."""
        ok = bool(getattr(result, "ok", False))
        error_type = getattr(result, "error_type", None) if result is not None else None
        if isinstance(error_type, str) and error_type:
            normalized_error_type = error_type
        else:
            lowered_error = str(getattr(result, "error", "") or "").lower()
            if any(marker in lowered_error for marker in cls._POLICY_ERROR_MARKERS):
                normalized_error_type = "policy"
            elif lowered_error:
                normalized_error_type = "generic"
            else:
                normalized_error_type = "none"
        return ok, normalized_error_type == "policy", normalized_error_type

    @staticmethod
    def record_agent_metric(app, agent, metric_name, latency):
        """Registra agent metric."""
        metrics = app.session_state.get("agent_metrics", {})
        if agent not in metrics:
            metrics[agent] = {
                "sent": 0,
                "received": 0,
                "succeeded": 0,
                "failed": 0,
                "latency": 0.0,
                "tool_calls_total": 0,
                "tool_calls_failed": 0,
                "invalid_tool_calls": 0,
                "tool_loop_abortions": 0,
                "tool_errors_by_type": {},
                "tool_loop_abort_reasons": {},
            }
        if metric_name == "sent":
            metrics[agent]["sent"] += 1
        elif metric_name == "received":
            metrics[agent]["received"] += 1
        elif metric_name == "succeeded":
            metrics[agent]["succeeded"] += 1
        elif metric_name == "failed":
            metrics[agent]["failed"] += 1
        if latency:
            metrics[agent]["latency"] += latency
        app.session_state["agent_metrics"] = metrics

        if hasattr(app, "behavior_metrics") and app.behavior_metrics and metric_name in ("succeeded", "failed"):
            app.behavior_metrics.record_response(
                agent,
                latency,
                has_next_step=metric_name == "succeeded",
                is_empty=metric_name == "failed",
            )

    @staticmethod
    def record_tool_event(
        app,
        agent,
        ok: bool,
        is_invalid: bool = False,
        loop_abort: bool = False,
        reason: str | None = None,
        error_type: str | None = None,
    ):
        """Registra métricas de uso de ferramentas por agente na sessão e no tracker persistido."""
        metrics = app.session_state.get("agent_metrics", {})
        if agent not in metrics:
            SessionMetricsService.record_agent_metric(app, agent, "received", 0.0)
            metrics = app.session_state.get("agent_metrics", {})
        agent_metrics = metrics[agent]
        agent_metrics.setdefault("tool_calls_total", 0)
        agent_metrics.setdefault("tool_calls_failed", 0)
        agent_metrics.setdefault("invalid_tool_calls", 0)
        agent_metrics.setdefault("tool_loop_abortions", 0)
        agent_metrics.setdefault("tool_errors_by_type", {})
        agent_metrics.setdefault("tool_loop_abort_reasons", {})

        if not loop_abort:
            agent_metrics["tool_calls_total"] += 1
            if not ok:
                agent_metrics["tool_calls_failed"] += 1
            if is_invalid:
                agent_metrics["invalid_tool_calls"] += 1
            normalized_error_type = str(error_type or "none")
            by_type = agent_metrics["tool_errors_by_type"]
            if normalized_error_type != "none" and not ok:
                by_type[normalized_error_type] = by_type.get(normalized_error_type, 0) + 1
        if loop_abort:
            agent_metrics["tool_loop_abortions"] += 1
            reason_key = str(reason or "unknown")
            reasons = agent_metrics["tool_loop_abort_reasons"]
            reasons[reason_key] = reasons.get(reason_key, 0) + 1
        app.session_state["agent_metrics"] = metrics

        if hasattr(app, "behavior_metrics") and app.behavior_metrics:
            if not loop_abort:
                app.behavior_metrics.record_tool_call(agent, ok=ok, is_invalid=is_invalid, error_type=error_type)
            if loop_abort:
                app.behavior_metrics.record_tool_loop_abort(agent, reason=reason)

    @staticmethod
    def has_clear_next_step(response):
        """Executa has clear next step."""
        if not response:
            return False
        response_lower = response.lower()
        indicators = [
            "próximo passo",
            "próxima etapa",
            "avançar",
            "continuar com",
            "a seguir",
            "para continuar",
            "próxima ação",
            "tarefa completa",
            "finalizado",
            "concluído",
            "done",
            "next step",
            "continuando",
        ]
        return any(ind in response_lower for ind in indicators)

    @staticmethod
    def is_response_redundant(response, history):
        """Executa is response redundant."""
        if not response or len(history) < 2:
            return False
        response_clean = response.lower().strip()
        recent_responses = [m["content"].lower().strip() for m in history[-3:] if m.get("role") != "human"]
        for past in recent_responses:
            if past and len(past) > 50 and len(response_clean) > 50:
                similarity = SequenceMatcher(None, past, response_clean).ratio()
                if similarity > 0.7:
                    return True
        return False

    def update_persisted_message_metrics(self, app, role, content):
        """Atualiza persisted message metrics."""
        if not hasattr(app, "session_state") or not app.session_state or role == "human":
            return
        try:
            app.session_state["total_responses"] = app.session_state.get("total_responses", 0) + 1
            has_next = self.has_clear_next_step(content)
            is_redundant = self.is_response_redundant(content, app.history)
            is_empty = not content or not content.strip()
            if has_next:
                app.session_state["responses_with_clear_next_step"] = app.session_state.get(
                    "responses_with_clear_next_step", 0
                ) + 1
            if is_redundant:
                app.session_state["consecutive_redundant_responses"] = app.session_state.get(
                    "consecutive_redundant_responses", 0
                ) + 1
            else:
                app.session_state["consecutive_redundant_responses"] = 0
            if hasattr(app, "behavior_metrics") and app.behavior_metrics:
                app.behavior_metrics.record_response(
                    role,
                    0.0,
                    has_next_step=has_next,
                    is_empty=is_empty,
                    is_redundant=is_redundant,
                    response_text=content,
                )
        except KeyError:
            pass
