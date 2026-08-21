"""Testes do comando `/stats` e do relatório estático de métricas."""
import json

from quimera.app.stats_services import StatsServices
from quimera.metrics import BehaviorMetricsTracker
from quimera.metrics_report import AgentStatsReporter


class MessageSink:
    """Coletor simples de mensagens exibidas ao humano."""

    def __init__(self):
        self.muted = []
        self.warnings = []

    def show_muted(self, message):
        self.muted.append(message)

    def show_warning(self, message):
        self.warnings.append(message)


def make_tracker() -> BehaviorMetricsTracker:
    """Cria um tracker com dados suficientes para acionar alertas."""
    tracker = BehaviorMetricsTracker()
    for _ in range(5):
        tracker.record_response("claude", 40.0, has_next_step=False, is_empty=True)
    for _ in range(4):
        tracker.record_synthesis("claude", needed_correction=True)
    tracker.record_tool_call("claude", ok=False, error_type="timeout")
    tracker.record_tool_call("claude", ok=True)
    tracker.record_response("codex", 2.0, has_next_step=True)
    return tracker


def make_service(tracker) -> tuple[StatsServices, MessageSink]:
    sink = MessageSink()
    service = StatsServices(
        metrics_tracker=tracker,
        show_muted_message=sink.show_muted,
        show_warning_message=sink.show_warning,
    )
    return service, sink


def test_overview_lists_every_tracked_agent():
    service, sink = make_service(make_tracker())

    assert service.handle_stats_command("/stats") is True

    payload = sink.muted[-1]
    assert "métricas de entrega (2 agente(s))" in payload
    assert "- claude |" in payload
    assert "- codex |" in payload


def test_agent_detail_includes_tool_breakdown():
    service, sink = make_service(make_tracker())

    assert service.handle_stats_command("/stats CLAUDE") is True

    payload = sink.muted[-1]
    assert payload.startswith("[stats] claude")
    assert "ferramentas: 2 chamadas" in payload
    assert "erros de ferramenta: timeout=1" in payload


def test_unknown_agent_reports_absence():
    service, sink = make_service(make_tracker())

    service.handle_stats_command("/stats gemini")

    assert sink.muted[-1] == "[stats] nenhuma métrica registrada para gemini."


def test_feedback_action_is_treated_as_agent_name():
    service, sink = make_service(make_tracker())

    service.handle_stats_command("/stats feedback")

    assert sink.muted[-1] == "[stats] nenhuma métrica registrada para feedback."


def test_reports_carry_no_warnings_section():
    service, sink = make_service(make_tracker())

    service.handle_stats_command("/stats")
    overview = sink.muted[-1]
    service.handle_stats_command("/stats claude")
    detail = sink.muted[-1]

    assert "atenção" not in overview
    assert "atenção" not in detail


def test_json_action_returns_parseable_payload():
    service, sink = make_service(make_tracker())

    service.handle_stats_command("/stats json claude")

    payload = json.loads(sink.muted[-1])
    assert payload["agent"] == "claude"
    assert payload["responses_total"] == 5

    service.handle_stats_command("/stats json")
    all_payload = json.loads(sink.muted[-1])
    assert [item["agent"] for item in all_payload] == ["claude", "codex"]


def test_overview_without_data_reports_empty():
    service, sink = make_service(BehaviorMetricsTracker())

    service.handle_stats_command("/stats")

    assert sink.muted[-1] == AgentStatsReporter.MSG_NO_DATA


def test_service_without_tracker_warns():
    service, sink = make_service(None)

    assert service.handle_stats_command("/stats") is True
    assert sink.warnings[-1] == "[stats] métricas não disponíveis nesta sessão."


def test_reporter_agent_detail_is_stable_for_untouched_agent():
    tracker = BehaviorMetricsTracker()
    reporter = AgentStatsReporter(tracker)

    assert reporter.render_agent("claude") == "[stats] nenhuma métrica registrada para claude."
    assert tracker.known_agents() == []


def test_reset_action_clears_single_agent():
    tracker = make_tracker()
    service, sink = make_service(tracker)

    assert service.handle_stats_command("/stats reset CLAUDE") is True

    assert sink.muted[-1] == "[stats] métricas de claude zeradas."
    assert tracker.known_agents() == ["codex"]


def test_reset_action_clears_every_agent():
    tracker = make_tracker()
    service, sink = make_service(tracker)

    service.handle_stats_command("/stats reset")

    assert sink.muted[-1] == "[stats] métricas zeradas (2 agente(s))."
    assert tracker.known_agents() == []


def test_reset_without_data_reports_empty():
    service, sink = make_service(BehaviorMetricsTracker())

    service.handle_stats_command("/stats reset")

    assert sink.muted[-1] == AgentStatsReporter.MSG_NO_DATA


def test_reset_of_unknown_agent_reports_absence():
    tracker = make_tracker()
    service, sink = make_service(tracker)

    service.handle_stats_command("/stats reset gemini")

    assert sink.muted[-1] == "[stats] nenhuma métrica registrada para gemini."
    assert tracker.known_agents() == ["claude", "codex"]


def test_reset_persists_removal_on_disk(tmp_path):
    storage = tmp_path / "metrics_state.json"
    tracker = BehaviorMetricsTracker(storage_path=storage)
    tracker.record_response("claude", 1.0, has_next_step=True)
    tracker.save(force=True)
    assert "claude" in json.loads(storage.read_text(encoding="utf-8"))

    service, sink = make_service(tracker)
    service.handle_stats_command("/stats reset")
    tracker.save(force=True)

    assert json.loads(storage.read_text(encoding="utf-8")) == {}
    assert BehaviorMetricsTracker(storage_path=storage).known_agents() == []
