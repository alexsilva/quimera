"""Serviços de gerenciamento e análise de bugs para QuimeraApp."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..bugs import (
    BugEvidenceRef,
    BugReport,
    make_bug_fingerprint,
)
from ..tasks.events import BugFiled
from .session_bootstrap import (
    resolve_workspace_render_ansi_path,
    resolve_workspace_render_log_path,
    resolve_workspace_metrics_path,
)

if TYPE_CHECKING:
    from .interfaces import IRenderer, IEventSink
    from ..bugs import BugStore, RenderBugDetector, AgentRuntimeBugDetector, BugCorrelator
    from ..workspace import Workspace
    from ..storage import SessionStorage

logger = logging.getLogger(__name__)


class BugServices:
    """Gerencia persistência, detecção automática e comandos de bugs."""

    def __init__(
        self,
        bug_store: BugStore,
        bug_detector: RenderBugDetector,
        agent_bug_detector: AgentRuntimeBugDetector,
        bug_correlator: BugCorrelator,
        workspace: Workspace,
        storage: SessionStorage,
        renderer: IRenderer,
        event_sink: IEventSink,
        show_system_message,
        show_warning_message,
        show_muted_message,
    ):
        self.bug_store = bug_store
        self.bug_detector = bug_detector
        self.agent_bug_detector = agent_bug_detector
        self.bug_correlator = bug_correlator
        self.workspace = workspace
        self.storage = storage
        self.renderer = renderer
        self.event_sink = event_sink
        self.show_system_message = show_system_message
        self.show_warning_message = show_warning_message
        self.show_muted_message = show_muted_message

    def file_bug(
        self,
        *,
        session_id: str,
        category: str,
        summary: str,
        severity: str = "medium",
        confidence: float = 0.5,
        description: str = "",
        agent: str = "",
        evidence_refs: list[BugEvidenceRef] | None = None,
    ) -> BugReport | None:
        """Registra um novo bug report e publica o evento correspondente."""
        if self.bug_store is None or not session_id or not category or not summary:
            return None
        fingerprint = make_bug_fingerprint(session_id, category, summary)
        report = BugReport(
            id=f"bug_{fingerprint[:12]}",
            session_id=session_id,
            category=category,
            summary=summary,
            severity=severity,
            confidence=confidence,
            description=description,
            fingerprint=fingerprint,
            evidence_refs=list(evidence_refs or []),
            agent=agent,
        )
        try:
            filed_report = self.bug_store.file(report)
        except Exception:
            logger.debug("falha ao persistir bug report", exc_info=True)
            return None
        if filed_report is not None:
            if self.event_sink is not None:
                try:
                    bug_event = BugFiled(
                        task_id=0,
                        job_id=0,
                        bug_id=filed_report.id,
                        category=filed_report.category,
                        summary=filed_report.summary,
                        severity=filed_report.severity,
                    )
                    self.event_sink.publish(bug_event)
                except Exception:
                    logger.debug("falha ao publicar BugFiled", exc_info=True)
        return filed_report

    def run_render_bug_detector(self, agent_metrics: dict | None = None) -> None:
        """Executa análise automática de bugs baseada em logs e métricas."""
        if self.bug_store is None or self.workspace is None or self.storage is None:
            return
        session_id = getattr(self.storage, "session_id", "")
        if not session_id:
            return
        events_path = resolve_workspace_render_log_path(self.workspace, session_id)
        ansi_path = resolve_workspace_render_ansi_path(self.workspace, session_id)
        metrics_path = resolve_workspace_metrics_path(self.workspace, session_id)
        try:
            all_reports: list[BugReport] = []
            if self.bug_detector is not None and (events_path is not None or ansi_path is not None):
                reports = self.bug_detector.analyze_session(
                    session_id=session_id,
                    events_path=events_path,
                    ansi_path=ansi_path,
                )
                for report in reports:
                    self.bug_store.file(report)
                all_reports.extend(reports)
            if self.agent_bug_detector is not None:
                reports = self.agent_bug_detector.analyze(
                    session_id=session_id,
                    agent_metrics=agent_metrics if isinstance(agent_metrics, dict) else {},
                    prompt_metrics_path=metrics_path,
                )
                for report in reports:
                    self.bug_store.file(report)
                all_reports.extend(reports)
            if self.bug_correlator is not None and len(all_reports) >= 2:
                for report in self.bug_correlator.correlate(all_reports, session_id=session_id):
                    self.bug_store.file(report)
        except Exception:
            logger.debug("falha ao analisar bugs de debug", exc_info=True)

    def handle_bugs_command(self, command: str, app_session_state: dict | None = None) -> bool:
        """Processa operações de bug report via `/bugs`."""
        raw = str(command or "").strip()
        parts = raw.split()
        action = parts[1].lower() if len(parts) >= 2 else "list"
        if self.bug_store is None:
            self.show_warning_message("[bugs] bug store não disponível.")
            return True
        try:
            session_id_actual = getattr(self.storage, "session_id", "")
            if action == "list":
                session_id = parts[2] if len(parts) >= 3 else session_id_actual
                reports = self.bug_store.query(session_id=session_id, status="open", limit=20) if session_id else self.bug_store.query(status="open", limit=20)
                if not reports:
                    self.show_system_message("[bugs] nenhum bug aberto.")
                    return True
                lines = [f"[bugs] abertos ({len(reports)}):"]
                for report in reports:
                    lines.append(f"- {report.id} | {report.severity} | {report.category} | count={report.count}")
                self.show_muted_message("\n".join(lines))
                return True

            if action == "show":
                if len(parts) < 3:
                    self.show_warning_message("Uso: /bugs show <bug_id>")
                    return True
                bug_id = parts[2].strip()
                reports = self.bug_store.query(limit=500)
                target = next((item for item in reports if item.id == bug_id), None)
                if target is None:
                    self.show_warning_message(f"[bugs] bug não encontrado: {bug_id}")
                    return True
                lines = [
                    f"[bugs] detalhes do bug {target.id}:",
                    f"  sessão: {target.session_id}",
                    f"  categoria: {target.category}",
                    f"  resumo: {target.summary}",
                    f"  severidade: {target.severity}",
                    f"  confiança: {target.confidence:.2f}",
                    f"  status: {target.status}",
                    f"  contagem: {target.count}",
                    f"  agente: {target.agent or '(desconhecido)'}",
                    f"  primeira ocorrência: {target.first_seen_at}",
                    f"  última ocorrência: {target.last_seen_at}",
                ]
                if target.description:
                    lines.append(f"  descrição: {target.description}")
                if target.evidence_refs:
                    evidence = target.evidence_refs[0]
                    location = evidence.path
                    if evidence.line is not None:
                        location = f"{location}:{evidence.line}"
                    elif evidence.offset is not None:
                        location = f"{location}:offset={evidence.offset}"
                    lines.append(f"  evidência: {evidence.kind} | {location}")
                    if evidence.preview:
                        lines.append(f"  preview: {evidence.preview[:200]}")
                self.show_muted_message("\n".join(lines))
                return True

            if action == "close":
                if len(parts) < 3:
                    self.show_warning_message("Uso: /bugs close <bug_id>")
                    return True
                bug_id = parts[2].strip()
                closed = self.bug_store.close_bug(bug_id)
                if closed is None:
                    self.show_warning_message(f"[bugs] bug não encontrado: {bug_id}")
                    return True
                self.show_system_message(f"[bugs] bug fechado: {closed.id}")
                return True

            if action == "analyze":
                if self.bug_detector is None and self.agent_bug_detector is None:
                    self.show_warning_message("[bugs] detectores não disponíveis.")
                    return True
                mode = "all"
                session_arg_index = 2
                if len(parts) >= 3 and parts[2].lower() in {"render", "agents", "all"}:
                    mode = parts[2].lower()
                    session_arg_index = 3
                session_id = parts[session_arg_index] if len(parts) > session_arg_index else session_id_actual
                if not session_id:
                    self.show_warning_message("[bugs] session_id inválido para análise.")
                    return True
                reports: list[BugReport] = []
                if mode in {"render", "all"}:
                    if self.bug_detector is None:
                        self.show_warning_message("[bugs] detector de render não disponível.")
                        return True
                    events_path = resolve_workspace_render_log_path(self.workspace, session_id)
                    ansi_path = resolve_workspace_render_ansi_path(self.workspace, session_id)
                    if events_path is None and ansi_path is None:
                        self.show_warning_message("[bugs] logs de render não encontrados para a sessão.")
                        return True
                    reports.extend(
                        self.bug_detector.analyze_session(
                            session_id=session_id,
                            events_path=events_path,
                            ansi_path=ansi_path,
                        )
                    )
                if mode in {"agents", "all"}:
                    if self.agent_bug_detector is None:
                        self.show_warning_message("[bugs] detector de agentes não disponível.")
                        return True
                    agent_metrics = (app_session_state or {}).get("agent_metrics", {})
                    metrics_path = resolve_workspace_metrics_path(self.workspace, session_id)
                    reports.extend(
                        self.agent_bug_detector.analyze(
                            session_id=session_id,
                            agent_metrics=agent_metrics if isinstance(agent_metrics, dict) else {},
                            prompt_metrics_path=metrics_path,
                        )
                    )
                filed = 0
                for report in reports:
                    if self.bug_store.file(report) is not None:
                        filed += 1
                if len(reports) >= 2:
                    if self.bug_correlator is not None:
                        for report in self.bug_correlator.correlate(reports, session_id=session_id):
                            if self.bug_store.file(report) is not None:
                                filed += 1
                self.show_system_message(
                    f"[bugs] análise ({mode}) concluída: {len(reports)} sinal(is), "
                    f"{filed} registro(s) processado(s)."
                )
                return True

            if action == "stats":
                session_id = parts[2] if len(parts) >= 3 else session_id_actual
                reports = (
                    self.bug_store.query(session_id=session_id, status="open", limit=500)
                    if session_id
                    else self.bug_store.query(status="open", limit=500)
                )
                if not reports:
                    self.show_system_message("[bugs] nenhum bug aberto.")
                    return True
                by_category: dict[str, int] = {}
                by_severity: dict[str, int] = {}
                by_agent: dict[str, int] = {}
                for report in reports:
                    by_category[report.category] = by_category.get(report.category, 0) + 1
                    sev = str(report.severity or "unknown")
                    by_severity[sev] = by_severity.get(sev, 0) + 1
                    agent_key = str(report.agent or "unknown")
                    by_agent[agent_key] = by_agent.get(agent_key, 0) + 1
                lines = [f"[bugs] stats ({len(reports)} abertos):", "por severidade:"]
                for severity, count in sorted(by_severity.items(), key=lambda item: (-item[1], item[0])):
                    lines.append(f"- {severity}: {count}")
                lines.append("por categoria:")
                for category, count in sorted(by_category.items(), key=lambda item: (-item[1], item[0])):
                    lines.append(f"- {category}: {count}")
                lines.append("por agente:")
                for agent_name, count in sorted(by_agent.items(), key=lambda item: (-item[1], item[0])):
                    lines.append(f"- {agent_name}: {count}")
                self.show_muted_message("\n".join(lines))
                return True
        except Exception:
            logger.exception("falha ao processar comando /bugs: %s", raw)
            self.show_warning_message("[bugs] falha interna ao processar comando.")
            return True

        self.show_warning_message("Uso: /bugs [list|show|close|analyze|stats] [args]")
        return True
