"""Testes para o sistema de métricas de comportamento dos agentes."""
import unittest

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
        self.assertEqual(metrics.invalid_handoff_rate, 0.0)
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

    def test_record_handoff(self):
        """Verifica registro de handoffs."""
        metrics = AgentBehaviorMetrics(agent_name="test")

        metrics.record_handoff_sent(is_invalid=False)
        metrics.record_handoff_sent(is_invalid=True)
        metrics.record_handoff_sent(is_invalid=False)
        metrics.record_handoff_received(is_circular=False)
        metrics.record_handoff_received(is_circular=True)

        self.assertEqual(metrics.handoffs_sent, 3)
        self.assertEqual(metrics.handoffs_invalid, 1)
        self.assertEqual(metrics.invalid_handoff_rate, 1 / 3)
        self.assertEqual(metrics.handoffs_received, 2)
        self.assertEqual(metrics.handoffs_circular_detected, 1)

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
        tracker.record_handoff_sent("claude", is_invalid=True)

        summary = tracker.get_agent_summary("claude")

        self.assertEqual(summary["agent"], "claude")
        self.assertEqual(summary["responses_total"], 2)
        self.assertEqual(summary["invalid_handoff_rate"], 1.0)

    def test_get_agent_summary_includes_tool_metrics(self):
        """Resumo deve incluir métricas explícitas de ferramenta."""
        tracker = BehaviorMetricsTracker()
        tracker.record_tool_call("claude", ok=True)
        tracker.record_tool_call("claude", ok=False, is_invalid=True)
        tracker.record_tool_loop_abort("claude")

        summary = tracker.get_agent_summary("claude")

        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_failed"], 1)
        self.assertEqual(summary["invalid_tool_calls"], 1)
        self.assertEqual(summary["tool_loop_abortions"], 1)
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

    def test_generate_feedback_low_data(self):
        """Verifica que não gera feedback com poucos dados."""
        tracker = BehaviorMetricsTracker()

        # Menos de 3 respostas
        tracker.record_response("claude", 1.0)
        tracker.record_response("claude", 2.0)

        feedback = tracker.generate_feedback("claude")
        self.assertEqual(feedback, "")

    def test_generate_feedback_high_invalid_rate(self):
        """Verifica feedback para taxa alta de handoffs inválidos."""
        tracker = BehaviorMetricsTracker()

        # 5 respostas com 3 handoffs inválidos de 5 (60%)
        for i in range(5):
            tracker.record_response("codex", 1.0, has_next_step=True)
            tracker.record_handoff_sent("codex", is_invalid=(i < 3))

        feedback = tracker.generate_feedback("codex")

        self.assertIn("HANDOFF INVÁLIDO", feedback)
        self.assertIn("60%", feedback)

    def test_generate_feedback_low_next_step_rate(self):
        """Verifica feedback para poucos próximos passos claros."""
        tracker = BehaviorMetricsTracker()

        # 10 respostas, apenas 2 com próximo passo (20%)
        for i in range(10):
            tracker.record_response("claude", 1.0, has_next_step=(i < 2))

        feedback = tracker.generate_feedback("claude")

        self.assertIn("PRÓXIMO PASSO", feedback)
        self.assertIn("20%", feedback)

    def test_generate_feedback_circular_detection(self):
        """Verifica feedback para delegações circulares."""
        tracker = BehaviorMetricsTracker()

        # Simular respostas e handoffs circulares
        for _ in range(4):
            tracker.record_response("codex", 1.0, has_next_step=True)
            tracker.record_handoff_received("codex", is_circular=True)

        feedback = tracker.generate_feedback("codex")

        self.assertIn("circulares", feedback.lower())

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
            tracker1.record_handoff_sent("claude", is_invalid=False)
            tracker1.record_synthesis("claude", needed_correction=True)

            # 2. Cria novo tracker com o mesmo path e verifica se carregou
            tracker2 = BehaviorMetricsTracker(storage_path=tmp_path)
            summary = tracker2.get_agent_summary("claude")

            self.assertEqual(summary["responses_total"], 1)
            self.assertEqual(summary["handoffs_sent"], 1)
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

    def test_get_position_summary(self):
        """Verifica geração de resumo de posição com vários alertas."""
        tracker = BehaviorMetricsTracker()
        metrics = tracker.get_agent("test")

        # Sem dados
        self.assertEqual(tracker.get_position_summary("test"), "")

        # Com dados e alertas
        for _ in range(10):
            metrics.record_response(40.0, is_empty=True)  # latency > 30, empty > 0.2
        metrics.record_handoff_sent(is_invalid=True)
        metrics.record_handoff_sent(is_invalid=True)  # rate > 0.3
        metrics.record_handoff_received(is_circular=True)
        for _ in range(4):
            metrics.record_synthesis(needed_correction=True)  # count >= 3, corrections > 0.5

        summary = tracker.get_position_summary("test")
        self.assertIn("HISTÓRICO", summary)
        self.assertIn("Atenção:", summary)
        self.assertIn("latência alta", summary)
        self.assertIn("handoffs inválidos", summary)
        self.assertIn("respostas vazias", summary)  # line 227
        self.assertIn("delegações circulares", summary)
        self.assertIn("sínteses com correção", summary)  # line 231

    def test_generate_feedback_for_tool_failures(self):
        """Feedback deve destacar falhas de tool use."""
        tracker = BehaviorMetricsTracker()
        for _ in range(5):
            tracker.record_response("tooly", 1.0, has_next_step=True)
        tracker.record_tool_call("tooly", ok=False, is_invalid=True)
        tracker.record_tool_call("tooly", ok=False)
        tracker.record_tool_call("tooly", ok=True)
        tracker.record_tool_loop_abort("tooly")

        feedback = tracker.generate_feedback("tooly")

        self.assertIn("FALHAS NO USO DE FERRAMENTAS", feedback)
        self.assertIn("FERRAMENTAS INVÁLIDAS", feedback)
        self.assertIn("LOOP DE FERRAMENTA ABORTADO", feedback)

    def test_generate_feedback_all_branches(self):
        """Verifica todos os ramos de feedback."""
        tracker = BehaviorMetricsTracker()
        metrics = tracker.get_agent("test")

        # 1. Respostas vazias
        for _ in range(5):
            metrics.record_response(1.0, is_empty=True)

        # 2. Redundâncias
        for _ in range(3):
            metrics.record_response(1.0, is_redundant=True)

        # 3. Síntese requer correção (line 293)
        for _ in range(4):
            metrics.record_synthesis(needed_correction=True)

        # 4. Baixa taxa de sucesso em handoffs (line 321)
        # handoffs_sent > 3
        for _ in range(5):
            metrics.record_handoff_sent(is_invalid=True)
        # success = 0/5 = 0.0 < 0.7

        feedback = tracker.generate_feedback("test")
        self.assertIn("RESPOSTAS VAZIAS", feedback)
        self.assertIn("RESPOSTAS REDUNDANTES", feedback)
        self.assertIn("SÍNTESES IMPRECISAS", feedback)
        self.assertIn("BAIXA TAXA DE SUCESSO", feedback)

    def test_generate_feedback_for_verbose_agent(self):
        """Feedback deve sugerir respostas mais curtas quando houver prolixidade."""
        tracker = BehaviorMetricsTracker()

        for _ in range(5):
            tracker.record_response("codex", 1.0, response_text="x" * 320)

        feedback = tracker.generate_feedback("codex")

        self.assertIn("RESPOSTAS LONGAS", feedback)
        self.assertIn("2-4 frases", feedback)


class TestPromptMetricsFeedback(unittest.TestCase):
    """Testes para integração de métricas no prompt — removidos após enxugamento do prompt."""

    def test_prompt_base_rules_are_concise(self):
        """Verifica que as regras base são concisas."""
        self.assertLess(len(prompt_template.base_rules), 1600)
        self.assertIn("humano", prompt_template.base_rules.lower())

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
