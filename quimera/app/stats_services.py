"""Serviço do comando `/stats`: consulta das métricas de comportamento."""
from __future__ import annotations

import logging

from ..metrics_report import AgentStatsReporter

logger = logging.getLogger(__name__)


class StatsServices:
    """Interpreta `/stats` e exibe os relatórios de métricas para o humano."""

    ACTION_JSON = "json"
    ACTION_RESET = "reset"
    ACTIONS = (ACTION_JSON, ACTION_RESET)

    def __init__(
        self,
        metrics_tracker,
        show_muted_message,
        show_warning_message,
    ):
        """Inicializa o serviço com o rastreador e os canais de exibição."""
        self.metrics_tracker = metrics_tracker
        self.show_muted_message = show_muted_message
        self.show_warning_message = show_warning_message

    @property
    def reporter(self) -> AgentStatsReporter | None:
        """Retorna o relator das métricas, ou None se não houver rastreador."""
        if self.metrics_tracker is None:
            return None
        return AgentStatsReporter(self.metrics_tracker)

    def handle_stats_command(self, command: str) -> bool:
        """Processa `/stats` e suas variações, sempre consumindo o comando."""
        reporter = self.reporter
        if reporter is None:
            self.show_warning_message("[stats] métricas não disponíveis nesta sessão.")
            return True

        parts = str(command or "").strip().split()
        arguments = parts[1:]
        try:
            self.show_muted_message(self._render(reporter, arguments))
        except Exception:
            logger.exception("falha ao renderizar /stats")
            self.show_warning_message("[stats] falha ao ler métricas.")
        return True

    def _render(self, reporter: AgentStatsReporter, arguments: list[str]) -> str:
        """Resolve a ação pedida e devolve o texto correspondente."""
        if not arguments:
            return reporter.render_overview()

        action = arguments[0].lower()
        target = self._resolve_agent(arguments[1]) if len(arguments) >= 2 else None

        if action == self.ACTION_JSON:
            return reporter.render_json(target)

        if action == self.ACTION_RESET:
            return reporter.render_reset(target)

        return reporter.render_agent(self._resolve_agent(action))

    def _resolve_agent(self, raw_name: str) -> str:
        """Resolve o nome informado pelo humano para o agente rastreado."""
        name = str(raw_name or "").strip().lstrip("/")
        lowered = name.lower()
        for known in self.metrics_tracker.known_agents():
            if known.lower() == lowered:
                return known
        return name
