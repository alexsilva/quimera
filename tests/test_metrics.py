"""Testes para o sistema de métricas de comportamento dos agentes."""
import unittest
from quimera.metrics import AgentBehaviorMetrics, BehaviorMetricsTracker


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
    
    def test_record_response(self):
        """Verifica registro de respostas."""
        metrics = AgentBehaviorMetrics(agent_name="test")
        
        metrics.record_response(1.5, has_next_step=True, is_empty=False)
        metrics.record_response(2.0, has_next_step=False, is_empty=True)
        metrics.record_response(1.0, has_next_step=True, is_empty=False)
        
        self.assertEqual(metrics.responses_total, 3)
        self.assertEqual(metrics.next_steps_claros, 2)
        self.assertEqual(metrics.responses_empty, 1)
        self.assertAlmostEqual(metrics.avg_latency_seconds, 1.5, places=2)
        self.assertAlmostEqual(metrics.next_step_clarity_rate, 2/3, places=2)
        self.assertAlmostEqual(metrics.empty_response_rate, 1/3, places=2)
    
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
        self.assertEqual(metrics.invalid_handoff_rate, 1/3)
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


class TestPromptMetricsFeedback(unittest.TestCase):
    """Testes para integração de métricas no prompt."""
    
    def test_prompt_includes_metrics_feedback_when_present(self):
        """Verifica que feedback é incluído quando fornecido."""
        from quimera.prompt import PromptBuilder
        
        class DummyContextManager:
            SUMMARY_MARKER = "<SUMMARY>"
            def load(self):
                return ""
            def load_session(self):
                return ""
        
        builder = PromptBuilder(DummyContextManager())
        history = [{"role": "human", "content": "Pergunta"}]
        
        metrics_feedback = "\nFEEDBACK OPERACIONAL:\n- Teste de feedback\n"
        prompt = builder.build("claude", history, metrics_feedback=metrics_feedback)
        
        self.assertIn("FEEDBACK OPERACIONAL", prompt)
        self.assertIn("Teste de feedback", prompt)
    
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
        
        prompt = builder.build("claude", history, metrics_feedback=None)
        
        self.assertNotIn("FEEDBACK OPERACIONAL", prompt)
        self.assertIn("REGRAS DE COLABORAÇÃO", prompt)


if __name__ == "__main__":
    unittest.main()
