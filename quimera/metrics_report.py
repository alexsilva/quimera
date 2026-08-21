"""Relatórios estáticos das métricas de entrega dos agentes.

As métricas continuam sendo coletadas por `BehaviorMetricsTracker` durante a
sessão, mas não são injetadas no prompt dos agentes. Este módulo apenas formata
os dados coletados para consulta pelo humano através do comando `/stats`, sem
gerar diagnósticos, alertas ou feedback.
"""
import json

from .metrics import BehaviorMetricsTracker


class AgentStatsReporter:
    """Formata as métricas de entrega dos agentes em texto consultável."""

    MSG_NO_DATA = "[stats] nenhuma métrica registrada ainda."
    USAGE = "Uso: /stats [<agente>|json [<agente>]|reset [<agente>]]"

    def __init__(self, tracker: BehaviorMetricsTracker):
        """Inicializa o relator sobre um rastreador de métricas."""
        self.tracker = tracker

    def render_overview(self) -> str:
        """Retorna uma linha resumida por agente com métricas acumuladas."""
        agents = self.tracker.known_agents()
        if not agents:
            return self.MSG_NO_DATA
        lines = [f"[stats] métricas de entrega ({len(agents)} agente(s)):"]
        for agent in agents:
            metrics = self.tracker.get_agent(agent)
            lines.append(
                f"- {agent} | turnos {metrics.responses_total}"
                f" | latência {metrics.avg_latency_seconds:.1f}s"
                f" | próximo passo {metrics.next_step_clarity_rate:.0%}"
                f" | vazias {metrics.empty_response_rate:.0%}"
                f" | tools {metrics.tool_calls_total} (sucesso {metrics.tool_success_rate:.0%})"
                f" | chars {metrics.avg_response_chars:.0f}"
            )
        lines.append(
            "Detalhe: /stats <agente> | bruto: /stats json [agente]"
            " | zerar: /stats reset [agente]"
        )
        return "\n".join(lines)

    def render_agent(self, agent_name: str) -> str:
        """Retorna o detalhamento completo das métricas de um agente."""
        if not self.tracker.has_metrics(agent_name):
            return f"[stats] nenhuma métrica registrada para {agent_name}."
        metrics = self.tracker.get_agent(agent_name)
        lines = [
            f"[stats] {agent_name}",
            f"  turnos: {metrics.responses_total}"
            f" (vazias {metrics.responses_empty}, {metrics.empty_response_rate:.0%})",
            f"  latência média: {metrics.avg_latency_seconds:.1f}s"
            f" (respostas cronometradas: {metrics.response_count})",
            f"  próximo passo claro: {metrics.next_steps_claros}"
            f" ({metrics.next_step_clarity_rate:.0%})",
            f"  tamanho médio: {metrics.avg_response_chars:.0f} chars"
            f" (longas {metrics.respostas_longas}, {metrics.long_response_rate:.0%})",
            f"  redundâncias: {metrics.redundancias_detectadas}",
            f"  contexto de código: {metrics.responses_code_context}",
            f"  delegações: {metrics.delegations_sent} enviadas"
            f" (inválidas {metrics.delegations_invalid}, {metrics.invalid_delegation_rate:.0%}),"
            f" {metrics.delegations_received} recebidas,"
            f" circulares {metrics.delegations_circular_detected}",
            f"  sínteses: {metrics.synthesis_requests} pedidos,"
            f" {metrics.synthesis_corrections} correções",
            f"  ferramentas: {metrics.tool_calls_total} chamadas,"
            f" falhas {metrics.tool_calls_failed},"
            f" sucesso {metrics.tool_success_rate:.0%},"
            f" inválidas {metrics.invalid_tool_calls},"
            f" loops abortados {metrics.tool_loop_abortions}",
        ]
        errors = self._format_counters(metrics.tool_errors_by_type)
        if errors:
            lines.append(f"  erros de ferramenta: {errors}")
        reasons = self._format_counters(metrics.tool_loop_abort_reasons)
        if reasons:
            lines.append(f"  motivos de aborto: {reasons}")
        return "\n".join(lines)

    def render_json(self, agent_name: str | None = None) -> str:
        """Retorna o resumo bruto em JSON, de um agente ou de todos."""
        if agent_name:
            if not self.tracker.has_metrics(agent_name):
                return f"[stats] nenhuma métrica registrada para {agent_name}."
            payload = self.tracker.get_agent_summary(agent_name)
        else:
            payload = self.tracker.get_all_summaries()
            if not payload:
                return self.MSG_NO_DATA
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def render_reset(self, agent_name: str | None = None) -> str:
        """Zera as métricas de um agente (ou de todos) e descreve o resultado."""
        if agent_name:
            if not self.tracker.reset_agent(agent_name):
                return f"[stats] nenhuma métrica registrada para {agent_name}."
            return f"[stats] métricas de {agent_name} zeradas."
        removed = self.tracker.reset_all()
        if not removed:
            return self.MSG_NO_DATA
        return f"[stats] métricas zeradas ({removed} agente(s))."

    @staticmethod
    def _format_counters(counters: dict) -> str:
        """Formata um dicionário de contadores como `chave=valor` ordenado."""
        if not counters:
            return ""
        return ", ".join(f"{key}={value}" for key, value in sorted(counters.items()))
