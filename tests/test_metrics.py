"""Testes para o sistema de métricas de comportamento dos agentes."""
import unittest
from types import SimpleNamespace

from quimera.app.session_metrics import SessionMetricsService
from quimera.domain.session_state import SessionRuntimeState
from quimera.metrics import AgentBehaviorMetrics, BehaviorMetricsTracker
from quimera.prompt_templates import prompt_template


class TestAgentBehaviorMetrics(unittest.TestCase):
    """Testes para AgentBehaviorMetrics."""

    def test_initial_state(self):
        """Verifica estado inicial das métricas."""
        metrics = AgentBehaviorMetrics(agent_name="test")

        self.assertEqual(metrics.agent_name, "test")
        self.assertEqual(metrics.responses_total, 0)
        self.assertEqual(metrics.avg_latency_seconds, 0.0)
        self.assertEqual(metrics.invalid_delegation_rate, 0.0)
        self.assertEqual(metrics.next_step_clarity_rate, 0.0)
        self.assertEqual(metrics.empty_response_rate, 0.0)

    def test_record_response(self):
        """Verifica registro de respostas."""
        metrics = AgentBehaviorMetrics(agent_name="test")

        metrics.record_response(
            1.5,
            has_next_step=True,
            is_empty=False,
            is_redundant=True,
            response_text="resposta curta",
        )
        metrics.record_response(2.0, has_next_step=False, is_empty=True)
        metrics.record_response(1.0, has_next_step=True, is_empty=False, response_text="ok")

        self.assertEqual(metrics.responses_total, 3)
        self.assertEqual(metrics.next_steps_claros, 2)
        self.assertEqual(metrics.responses_empty, 1)
        self.assertEqual(metrics.redundancias_detectadas, 1)
        self.assertEqual(metrics.respostas_longas, 0)
        self.assertGreater(metrics.avg_response_chars, 0.0)
        self.assertAlmostEqual(metrics.avg_latency_seconds, 1.5, places=2)
        self.assertAlmostEqual(metrics.next_step_clarity_rate, 2 / 3, places=2)
        self.assertAlmostEqual(metrics.empty_response_rate, 1 / 3, places=2)

    def test_record_delegation(self):
        """Verifica registro de delegations."""
        metrics = AgentBehaviorMetrics(agent_name="test")

        metrics.record_delegation_sent(is_invalid=False)
        metrics.record_delegation_sent(is_invalid=True)
        metrics.record_delegation_sent(is_invalid=False)
        metrics.record_delegation_received(is_circular=False)
        metrics.record_delegation_received(is_circular=True)

        self.assertEqual(metrics.delegations_sent, 3)
        self.assertEqual(metrics.delegations_invalid, 1)
        self.assertEqual(metrics.invalid_delegation_rate, 1 / 3)
        self.assertEqual(metrics.delegations_received, 2)
        self.assertEqual(metrics.delegations_circular_detected, 1)

    def test_record_synthesis(self):
        """Verifica registro de sínteses."""
        metrics = AgentBehaviorMetrics(agent_name="test")

        metrics.record_synthesis(needed_correction=False)
        metrics.record_synthesis(needed_correction=True)
        metrics.record_synthesis(needed_correction=True)

        self.assertEqual(metrics.synthesis_requests, 3)
        self.assertEqual(metrics.synthesis_corrections, 2)

    def test_record_tool_metrics(self):
        """Verifica registro de métricas de ferramentas."""
        metrics = AgentBehaviorMetrics(agent_name="test")

        metrics.record_tool_call(ok=True)
        metrics.record_tool_call(ok=False, is_invalid=True)
        metrics.record_tool_loop_abort()

        self.assertEqual(metrics.tool_calls_total, 2)
        self.assertEqual(metrics.tool_calls_failed, 1)
        self.assertEqual(metrics.invalid_tool_calls, 1)
        self.assertEqual(metrics.tool_loop_abortions, 1)
        self.assertAlmostEqual(metrics.tool_success_rate, 0.5, places=2)

    def test_to_from_dict(self):
        """Verifica serialização e desserialização."""
        metrics = AgentBehaviorMetrics(agent_name="test", responses_total=10)
        data = metrics.to_dict()
        self.assertEqual(data["agent_name"], "test")
        self.assertEqual(data["responses_total"], 10)

        metrics2 = AgentBehaviorMetrics.from_dict(data)
        self.assertEqual(metrics2.agent_name, "test")
        self.assertEqual(metrics2.responses_total, 10)


class TestBehaviorMetricsTracker(unittest.TestCase):
    """Testes para BehaviorMetricsTracker."""

    def test_get_agent_creates_if_not_exists(self):
        """Verifica criação automática de métricas para agente novo."""
        tracker = BehaviorMetricsTracker()

        metrics = tracker.get_agent("claude")
        self.assertEqual(metrics.agent_name, "claude")

        # Segunda chamada retorna o mesmo objeto
        metrics2 = tracker.get_agent("claude")
        self.assertIs(metrics, metrics2)

    def test_get_agent_summary(self):
        """Verifica geração de resumo."""
        tracker = BehaviorMetricsTracker()

        tracker.record_response("claude", 2.0, has_next_step=True)
        tracker.record_response("claude", 3.0, is_empty=True)
        tracker.record_delegation_sent("claude", is_invalid=True)

        summary = tracker.get_agent_summary("claude")

        self.assertEqual(summary["agent"], "claude")
        self.assertEqual(summary["responses_total"], 2)
        self.assertEqual(summary["invalid_delegation_rate"], 1.0)

    def test_get_agent_summary_includes_tool_metrics(self):
        """Resumo deve incluir métricas explícitas de ferramenta."""
        tracker = BehaviorMetricsTracker()
        tracker.record_tool_call("claude", ok=True)
        tracker.record_tool_call("claude", ok=False, is_invalid=True, error_type="policy")
        tracker.record_tool_loop_abort("claude", reason="invalid_tool_loop")

        summary = tracker.get_agent_summary("claude")

        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_failed"], 1)
        self.assertEqual(summary["invalid_tool_calls"], 1)
        self.assertEqual(summary["tool_loop_abortions"], 1)
        self.assertEqual(summary["tool_errors_by_type"]["policy"], 1)
        self.assertEqual(summary["tool_loop_abort_reasons"]["invalid_tool_loop"], 1)
        self.assertEqual(summary["tool_success_rate"], 0.5)

    def test_get_agent_summary_includes_verbosity_metrics(self):
        """Resumo deve incluir métricas de verbosidade."""
        tracker = BehaviorMetricsTracker()
        tracker.record_response("claude", 1.0, response_text="x" * 320)
        tracker.record_response("claude", 1.0, response_text="curta")

        summary = tracker.get_agent_summary("claude")

        self.assertEqual(summary["respostas_longas"], 1)
        self.assertEqual(summary["long_response_rate"], 0.5)
        self.assertGreater(summary["avg_response_chars"], 100)

    def test_persistence(self):
        """Verifica se o ciclo save -> reload -> load funciona corretamente."""
        import tempfile
        import os
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # 1. Cria tracker, grava dados e salva
            tracker1 = BehaviorMetricsTracker(storage_path=tmp_path)
            tracker1.record_response("claude", 2.0, has_next_step=True)
            tracker1.record_delegation_sent("claude", is_invalid=False)
            tracker1.record_synthesis("claude", needed_correction=True)

            # 2. Cria novo tracker com o mesmo path e verifica se carregou
            tracker2 = BehaviorMetricsTracker(storage_path=tmp_path)
            summary = tracker2.get_agent_summary("claude")

            self.assertEqual(summary["responses_total"], 1)
            self.assertEqual(summary["delegations_sent"], 1)
            self.assertEqual(summary["synthesis_corrections"], 1)
            self.assertEqual(summary["agent"], "claude")

            # 3. Adiciona mais dados e salva de novo
            tracker2.record_response("codex", 1.5)

            tracker3 = BehaviorMetricsTracker(storage_path=tmp_path)
            self.assertEqual(tracker3.get_agent_summary("codex")["responses_total"], 1)
            self.assertEqual(tracker3.get_agent_summary("claude")["responses_total"], 1)

        finally:
            if tmp_path.exists():
                os.unlink(tmp_path)

    def test_load_corrupt_json(self):
        """Verifica que o carregamento ignora JSON corrompido."""
        import tempfile
        import os
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp_path.write_text("invalid json", encoding="utf-8")

        try:
            tracker = BehaviorMetricsTracker(storage_path=tmp_path)
            self.assertEqual(len(tracker._metrics), 0)
        finally:
            if tmp_path.exists():
                os.unlink(tmp_path)

    def test_save_exception(self):
        """Verifica tratamento de exceção ao salvar."""
        # Path que não pode ser criado (root as file?)
        tracker = BehaviorMetricsTracker(storage_path="/dev/null/metrics.json")
        tracker.get_agent("test")
        # should not raise, just print to stderr
        tracker.save()

    def test_get_all_summaries(self):
        """Verifica listagem de todos os resumos."""
        tracker = BehaviorMetricsTracker()
        tracker.get_agent("b")
        tracker.get_agent("a")
        summaries = tracker.get_all_summaries()
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0]["agent"], "a")
        self.assertEqual(summaries[1]["agent"], "b")

    def test_feedback_layer_is_absent(self):
        """A camada de feedback/diagnóstico não existe mais no tracker."""
        tracker = BehaviorMetricsTracker()
        for name in ("generate_feedback", "get_position_summary", "collect_warnings"):
            self.assertFalse(hasattr(tracker, name), name)


class TestSessionMetricsService(unittest.TestCase):
    """Integração entre classificação de resposta e estado canônico da sessão."""

    def test_redundant_response_is_counted_once_without_clear_next_step(self):
        repeated = "Resposta suficientemente longa e repetida para caracterizar redundância no histórico da sessão."
        state = SessionRuntimeState()
        app = SimpleNamespace(
            session_state=state,
            history=[
                {"role": "assistant", "content": repeated},
                {"role": "assistant", "content": repeated},
            ],
            behavior_metrics=None,
        )

        SessionMetricsService().update_persisted_message_metrics(app, "assistant", repeated)

        self.assertEqual(state.metrics.total_responses, 1)
        self.assertEqual(state.metrics.responses_with_clear_next_step, 0)
        self.assertEqual(state.metrics.consecutive_redundant_responses, 1)

    def test_agent_metric_uses_response_text_for_behavior_metrics(self):
        """Com texto disponível, tamanho e próximo passo vêm do conteúdo real."""
        tracker = BehaviorMetricsTracker()
        app = SimpleNamespace(
            session_state={},
            history=[],
            behavior_metrics=tracker,
        )
        text = "Ajuste aplicado em quimera/metrics.py. Próximo passo: rodar a suíte."

        SessionMetricsService.record_agent_metric(app, "claude", "succeeded", 2.0, text)

        metrics = tracker.get_agent("claude")
        self.assertEqual(metrics.responses_total, 1)
        self.assertEqual(metrics.responses_empty, 0)
        self.assertEqual(metrics.next_steps_claros, 1)
        self.assertEqual(metrics.avg_response_chars, float(len(text)))
        self.assertEqual(metrics.responses_code_context, 1)

    def test_agent_metric_marks_empty_text_as_empty_response(self):
        """Texto vazio conta como resposta vazia mesmo com dispatch bem-sucedido."""
        tracker = BehaviorMetricsTracker()
        app = SimpleNamespace(session_state={}, history=[], behavior_metrics=tracker)

        SessionMetricsService.record_agent_metric(app, "codex", "succeeded", 1.0, "   ")

        metrics = tracker.get_agent("codex")
        self.assertEqual(metrics.responses_empty, 1)
        self.assertEqual(metrics.next_steps_claros, 0)

    def test_agent_metric_without_text_keeps_dispatch_semantics(self):
        """Sem texto (chamador legado), o resultado do dispatch continua valendo."""
        tracker = BehaviorMetricsTracker()
        app = SimpleNamespace(session_state={}, history=[], behavior_metrics=tracker)

        SessionMetricsService.record_agent_metric(app, "codex", "failed", 0.0)

        metrics = tracker.get_agent("codex")
        self.assertEqual(metrics.responses_empty, 1)
        self.assertEqual(metrics.total_response_chars, 0)


class TestPromptMetricsFeedback(unittest.TestCase):
    """Testes para integração de métricas no prompt — removidos após enxugamento do prompt."""

    def test_prompt_base_rules_are_concise(self):
        """Verifica que as regras base estão inline no template e são concisas."""
        main = prompt_template._load()
        self.assertLess(len(main), 12000)
        self.assertIn("humano", main.lower())

    def test_prompt_without_metrics_feedback(self):
        """Verifica que prompt funciona sem feedback."""
        from quimera.prompt import PromptBuilder

        class DummyContextManager:
            SUMMARY_MARKER = "<SUMMARY>"

            def load(self):
                return ""

            def load_session(self):
                return ""

        builder = PromptBuilder(DummyContextManager())
        history = [{"role": "human", "content": "Pergunta"}]

        prompt = builder.build("claude", history)

        self.assertNotIn("FEEDBACK OPERACIONAL", prompt)
        self.assertIn("humano", prompt.lower())


if __name__ == "__main__":
    unittest.main()
