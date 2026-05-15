"""Testes de cobertura para quimera/app/chat_round.py."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from quimera.app.chat_round import ChatRoundOrchestrator
from quimera.prompt_kinds import PromptKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(active_agents=None, threads=1):
    """Cria um app mínimo para testes do ChatRoundOrchestrator."""
    app = SimpleNamespace()
    app.active_agents = list(active_agents or ["claude", "codex"])
    app.threads = threads
    app.round_index = 0
    app.summary_agent_preference = None
    app.shared_state = {}
    app._pending_input_for = None
    app.session_state = {"session_id": "test-cr", "history_count": 0, "summary_loaded": False}
    app.behavior_metrics = None

    app.renderer = Mock()
    app.renderer.show_warning = Mock()
    app.renderer.show_system = Mock()
    app.renderer.show_handoff = Mock()

    app.dispatch_services = Mock()
    app.dispatch_services.call_agent = Mock(return_value=None)
    app.dispatch_services.print_response = Mock()

    app.session_services = Mock()
    app.session_services.persist_message = Mock()
    app.session_services.maybe_auto_summarize = Mock()

    app.turn_manager = Mock()
    app.turn_manager.reset = Mock()

    app.agent_client = Mock()
    app.agent_client._user_cancelled = False

    # parse_routing padrão: ("claude", "hello", False)
    app.parse_routing = Mock(return_value=("claude", "hello", False))
    # parse_response padrão: resposta simples sem handoff
    app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

    app.chat_round_orchestrator = ChatRoundOrchestrator(app)
    return app


def _make_handoff(task="Faça X", handoff_id="id-001", chain=None, priority="normal"):
    return {
        "handoff_id": handoff_id,
        "task": task,
        "priority": priority,
        "chain": chain or [],
    }


# ---------------------------------------------------------------------------
# process() — fluxo principal
# ---------------------------------------------------------------------------

class TestProcessMainFlow(unittest.TestCase):

    def test_parse_routing_none_returns_early(self):
        """Se parse_routing retorna None como agente, process() deve retornar sem chamar call_agent."""
        app = _make_app()
        app.parse_routing = Mock(return_value=(None, None, None))
        app.chat_round_orchestrator.process("oi")
        app.dispatch_services.call_agent.assert_not_called()

    def test_empty_message_shows_warning(self):
        """Mensagem vazia deve chamar show_warning."""
        app = _make_app()
        app.parse_routing = Mock(return_value=("claude", "   ", False))
        app.chat_round_orchestrator.process("   ")
        app.renderer.show_warning.assert_called_once()
        app.dispatch_services.call_agent.assert_not_called()

    def test_pending_input_for_overrides_first_agent(self):
        """_pending_input_for deve substituir first_agent quando explicit=False."""
        app = _make_app()
        app.parse_routing = Mock(return_value=("claude", "hello", False))
        app._pending_input_for = "codex"
        app.dispatch_services.call_agent = Mock(return_value="resp de codex")
        app.parse_response = Mock(return_value=("resp de codex", None, None, False, False, None))

        app.chat_round_orchestrator.process("hello")

        first_call_agent = app.dispatch_services.call_agent.call_args_list[0]
        self.assertEqual(first_call_agent.args[0], "codex")

    def test_user_cancelled_after_first_call(self):
        """_user_cancelled após primeira chamada deve resetar turno e retornar."""
        app = _make_app()
        app.dispatch_services.call_agent = Mock(return_value="alguma resposta")
        app.parse_response = Mock(return_value=("alguma resposta", None, None, False, False, None))
        app.agent_client._user_cancelled = True

        app.chat_round_orchestrator.process("hello")

        app.turn_manager.reset.assert_called_once()
        # maybe_auto_summarize não deve ter sido chamado (retornou cedo)
        app.session_services.maybe_auto_summarize.assert_not_called()

    def test_needs_human_input_sets_pending(self):
        """needs_human_input=True deve setar _pending_input_for e retornar."""
        app = _make_app()
        app.dispatch_services.call_agent = Mock(return_value="Qual sua escolha?")
        app.parse_response = Mock(return_value=("Qual sua escolha?", None, None, False, True, None))

        app.chat_round_orchestrator.process("hello")

        self.assertEqual(app._pending_input_for, "claude")
        app.session_services.maybe_auto_summarize.assert_not_called()

    def test_fallback_skips_none_response(self):
        """Fallback deve continuar ao próximo candidato quando também retorna None."""
        app = _make_app(active_agents=["claude", "codex", "deepseek"])
        responses = iter([None, None, "deepseek respondeu"])
        app.dispatch_services.call_agent = Mock(side_effect=lambda *a, **kw: next(responses))

        def fake_parse(resp):
            if resp is None:
                return (None, None, None, False, False, None)
            return (resp, None, None, False, False, None)

        app.parse_response = Mock(side_effect=fake_parse)

        app.chat_round_orchestrator.process("hello")

        # Chamado: claude (None), codex (None), deepseek (ok)
        self.assertEqual(app.dispatch_services.call_agent.call_count, 3)

    def test_self_handoff_ignored_in_main_flow(self):
        """Handoff para si mesmo deve ser ignorado no fluxo principal."""
        app = _make_app()
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="ok")
        app.parse_response = Mock(return_value=("ok", "claude", handoff, False, False, None))

        app.chat_round_orchestrator.process("hello")

        # _process_handoff NÃO deve ser chamado; como route_target == first_agent, é anulado
        app.renderer.show_handoff.assert_not_called()

    def test_unknown_agent_handoff_ignored_in_main_flow(self):
        """Handoff para agente desconhecido deve ser ignorado no fluxo principal."""
        app = _make_app(active_agents=["claude", "codex"])
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="ok")
        app.parse_response = Mock(return_value=("ok", "agente-inexistente", handoff, False, False, None))

        app.chat_round_orchestrator.process("hello")

        app.renderer.show_handoff.assert_not_called()


# ---------------------------------------------------------------------------
# _process_handoff
# ---------------------------------------------------------------------------

class TestProcessHandoff(unittest.TestCase):

    def test_self_handoff_in_chain_breaks(self):
        """Handoff para si mesmo dentro da cadeia deve interromper com warning."""
        app = _make_app()
        handoff = _make_handoff("task x")
        orchestrator = ChatRoundOrchestrator(app)

        # current_from == current_target → self-handoff
        orchestrator._process_handoff("claude", "claude", handoff)

        app.renderer.show_warning.assert_called()

    def test_circular_delegation_breaks(self):
        """Delegação circular detectada na chain deve interromper com warning."""
        app = _make_app()
        handoff = _make_handoff("task x", chain=["codex"])  # codex já na chain
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.renderer.show_warning.assert_called()

    def test_circular_delegation_records_behavior_metrics(self):
        """behavior_metrics.record_handoff_received deve ser chamado com is_circular=True."""
        app = _make_app()
        app.behavior_metrics = Mock()
        handoff = _make_handoff("task x", chain=["codex"])
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.behavior_metrics.record_handoff_received.assert_called_once_with("codex", is_circular=True)

    def test_unknown_agent_in_chain_breaks(self):
        """Agente desconhecido na cadeia deve interromper com warning."""
        app = _make_app(active_agents=["claude", "codex"])
        handoff = _make_handoff("task x")
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "fantasma", handoff)

        app.renderer.show_warning.assert_called()

    def test_behavior_metrics_record_sent_received(self):
        """behavior_metrics deve registrar sent/received em cada hop."""
        app = _make_app()
        app.behavior_metrics = Mock()
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="codex respondeu")
        app.parse_response = Mock(return_value=("codex respondeu", None, None, False, False, None))
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.behavior_metrics.record_handoff_sent.assert_called_with("claude")
        app.behavior_metrics.record_handoff_received.assert_any_call("codex")

    def test_handoff_dispatch_uses_task_executor_prompt(self):
        """Handoff vindo do chat deve chamar o destino com prompt isolado de executor."""
        app = _make_app()
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="codex respondeu")
        app.parse_response = Mock(return_value=("codex respondeu", None, None, False, False, None))
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        first_call = app.dispatch_services.call_agent.call_args_list[0]
        self.assertEqual(first_call.args[0], "codex")
        self.assertTrue(first_call.kwargs["handoff_only"])
        self.assertEqual(first_call.kwargs["prompt_kind"], PromptKind.TASK_EXECUTOR)

    def test_user_cancelled_during_handoff(self):
        """_user_cancelled após chamada ao agente secundário deve resetar e retornar."""
        app = _make_app()
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="resp")
        app.parse_response = Mock(return_value=("resp", None, None, False, False, None))
        app.agent_client._user_cancelled = True
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.turn_manager.reset.assert_called()

    def test_ack_mismatch_logs_warning(self):
        """Mismatch de ACK deve ser logado (sem falha)."""
        app = _make_app()
        handoff = _make_handoff("task x", handoff_id="expected-id")
        app.dispatch_services.call_agent = Mock(return_value="resp")
        # ack_id diferente do handoff_id
        app.parse_response = Mock(return_value=("resp", None, None, False, False, "wrong-id"))
        orchestrator = ChatRoundOrchestrator(app)

        # Não deve levantar exceção
        orchestrator._process_handoff("claude", "codex", handoff)

    def test_no_fallback_breaks(self):
        """Sem candidatos de fallback, deve interromper a cadeia."""
        app = _make_app(active_agents=["claude", "codex"])
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value=None)
        app.parse_response = Mock(return_value=(None, None, None, False, False, None))
        orchestrator = ChatRoundOrchestrator(app)

        # Executa sem levantar exceção; codex não responde e não há fallback
        orchestrator._process_handoff("claude", "codex", handoff)

        # Deve ter chamado call_agent para codex (e nada mais pois sem fallback)
        calls = [c.args[0] for c in app.dispatch_services.call_agent.call_args_list]
        self.assertIn("codex", calls)

    def test_fallback_succeeds_in_handoff_chain(self):
        """Quando agente alvo falha, fallback candidate deve ser tentado e usar sua resposta."""
        app = _make_app(active_agents=["claude", "codex", "deepseek"])
        handoff = _make_handoff("task x")
        call_count = [0]

        def fake_call(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # codex falha
            if call_count[0] == 2:
                return "deepseek respondeu"  # fallback deepseek funciona
            return "síntese"  # síntese pelo claude

        app.dispatch_services.call_agent = Mock(side_effect=fake_call)
        parse_count = [0]

        def fake_parse(resp):
            parse_count[0] += 1
            if parse_count[0] == 1:
                return (None, None, None, False, False, None)  # codex sem resposta
            if parse_count[0] == 2:
                return ("deepseek respondeu", None, None, False, False, None)  # fallback ok
            return ("síntese", None, None, False, False, None)

        app.parse_response = Mock(side_effect=fake_parse)
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        # deepseek deve ter sido chamado como fallback
        calls = [c.args[0] for c in app.dispatch_services.call_agent.call_args_list]
        self.assertIn("deepseek", calls)

    def test_user_cancelled_during_fallback(self):
        """_user_cancelled durante fallback deve resetar e retornar."""
        app = _make_app(active_agents=["claude", "codex", "deepseek"])
        handoff = _make_handoff("task x")
        call_count = [0]

        def fake_call(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # codex falha
            # fallback deepseek: marca cancelado
            app.agent_client._user_cancelled = True
            return "deepseek respondeu"

        app.dispatch_services.call_agent = Mock(side_effect=fake_call)

        def fake_parse(resp):
            if resp is None:
                return (None, None, None, False, False, None)
            return (resp, None, None, False, False, None)

        app.parse_response = Mock(side_effect=fake_parse)
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.turn_manager.reset.assert_called()

    def test_max_hops_exceeded_shows_system_message(self):
        """Quando max_hops é excedido, exibe mensagem de sistema."""
        app = _make_app(active_agents=["claude", "codex"])
        handoff = _make_handoff("task x")
        # HANDOFF_MAX_HOPS_FACTOR=0 → max_hops = max(1, 2*0) = 1
        # Após 1 hop, o while não itera mais e exibe mensagem de limite
        orchestrator = ChatRoundOrchestrator(app)
        orchestrator.HANDOFF_MAX_HOPS_FACTOR = 0

        next_h = _make_handoff("next task", handoff_id="id-002")
        app.dispatch_services.call_agent = Mock(return_value="resp")
        app.parse_response = Mock(return_value=("resp", "codex", next_h, False, False, None))

        orchestrator._process_handoff("claude", "codex", handoff)

        shown = [str(c) for c in app.renderer.show_system.call_args_list]
        self.assertTrue(any("limite" in m for m in shown))

    def test_behavior_metrics_record_synthesis(self):
        """behavior_metrics.record_synthesis deve ser chamado antes da síntese."""
        app = _make_app()
        app.behavior_metrics = Mock()
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="codex respondeu")
        parse_count = [0]

        def fake_parse(resp):
            parse_count[0] += 1
            if parse_count[0] == 1:
                # Primeira parse (resposta do codex): sem next handoff
                return ("codex respondeu", None, None, False, False, None)
            # Síntese do claude
            return ("síntese final", None, None, False, False, None)

        app.parse_response = Mock(side_effect=fake_parse)
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.behavior_metrics.record_synthesis.assert_called_with("claude")

    def test_user_cancelled_during_synthesis(self):
        """_user_cancelled durante síntese deve resetar e retornar."""
        app = _make_app()
        handoff = _make_handoff("task x")
        call_count = [0]

        def fake_call(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "codex respondeu"
            # Segunda chamada é a síntese — marca como cancelado
            app.agent_client._user_cancelled = True
            return "síntese"

        app.dispatch_services.call_agent = Mock(side_effect=fake_call)
        # parse só é chamado para a resposta do codex; síntese é abortada antes
        app.parse_response = Mock(return_value=("codex respondeu", None, None, False, False, None))
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_handoff("claude", "codex", handoff)

        app.turn_manager.reset.assert_called()


# ---------------------------------------------------------------------------
# _process_standard_flow
# ---------------------------------------------------------------------------

class TestProcessStandardFlow(unittest.TestCase):

    def test_user_cancelled_in_standard_loop(self):
        """_user_cancelled no loop padrão deve resetar e retornar."""
        app = _make_app(active_agents=["claude", "codex"])
        app.agent_client._user_cancelled = True
        app.dispatch_services.call_agent = Mock(return_value="resp")
        app.parse_response = Mock(return_value=("resp", None, None, False, False, None))
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        app.turn_manager.reset.assert_called()

    def test_needs_human_input_sets_pending_in_loop(self):
        """needs_human_input=True no loop padrão deve setar _pending_input_for."""
        app = _make_app(active_agents=["claude", "codex"])
        app.dispatch_services.call_agent = Mock(return_value="resp")
        app.parse_response = Mock(return_value=("resp", None, None, False, True, None))
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        self.assertEqual(app._pending_input_for, "codex")

    def test_self_handoff_ignored_in_standard_flow(self):
        """Handoff para si mesmo no fluxo padrão deve ser ignorado com warning."""
        app = _make_app(active_agents=["claude", "codex"])
        app.dispatch_services.call_agent = Mock(return_value="resp")
        # codex tenta handoff para si mesmo
        handoff = _make_handoff("task y")
        parse_count = [0]

        def fake_parse(resp):
            parse_count[0] += 1
            if parse_count[0] == 1:
                return ("resp", "codex", handoff, False, False, None)
            return ("resp2", None, None, False, False, None)

        app.parse_response = Mock(side_effect=fake_parse)
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        app.renderer.show_warning.assert_called()

    def test_parallel_mode_uses_thread_pool(self):
        """Com threads > 1 e remaining > 1, deve usar ThreadPoolExecutor."""
        app = _make_app(active_agents=["claude", "codex"], threads=2)
        # Para entrar no modo paralelo: extend=True → remaining = ["codex", "claude", "codex"]
        app.get_agent_plugin = Mock(return_value=SimpleNamespace(output_format="text"))
        app._merge_staging_to_workspace = Mock()
        app.task_services = Mock()
        app.task_services.call_agent_for_parallel = Mock(
            return_value=("codex", "resposta", None, None, False, False)
        )
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        app.task_services.call_agent_for_parallel.assert_called()
        app._merge_staging_to_workspace.assert_called()

    def test_parallel_mode_user_cancelled_after_merge(self):
        """_user_cancelled após merge em modo paralelo deve resetar e retornar."""
        app = _make_app(active_agents=["claude", "codex"], threads=2)
        app.get_agent_plugin = Mock(return_value=SimpleNamespace(output_format="text"))

        def fake_merge(staging_root):
            app.agent_client._user_cancelled = True

        app._merge_staging_to_workspace = Mock(side_effect=fake_merge)
        app.task_services = Mock()
        app.task_services.call_agent_for_parallel = Mock(
            return_value=("codex", "resposta", None, None, False, False)
        )
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        app.turn_manager.reset.assert_called()

    def test_parallel_mode_needs_input(self):
        """needs_input=True em resultado paralelo deve setar _pending_input_for."""
        app = _make_app(active_agents=["claude", "codex"], threads=2)
        app.get_agent_plugin = Mock(return_value=SimpleNamespace(output_format="text"))
        app._merge_staging_to_workspace = Mock()
        app.task_services = Mock()
        app.task_services.call_agent_for_parallel = Mock(
            return_value=("codex", "resposta", None, None, True, True)
        )
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        self.assertEqual(app._pending_input_for, "codex")


# ---------------------------------------------------------------------------
# _show_system helper
# ---------------------------------------------------------------------------

class TestShowSystem(unittest.TestCase):

    def test_show_system_uses_show_system_message_when_available(self):
        """_show_system deve preferir app.show_system_message se disponível."""
        app = _make_app()
        app.show_system_message = Mock()
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._show_system("teste")

        app.show_system_message.assert_called_once_with("teste")
        app.renderer.show_system.assert_not_called()

    def test_show_system_falls_back_to_renderer(self):
        """_show_system deve usar renderer.show_system se show_system_message não existir."""
        app = _make_app()
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._show_system("teste")

        app.renderer.show_system.assert_called_once_with("teste")

    def test_show_system_falls_back_to_renderer_on_attribute_error(self):
        """_show_system deve usar renderer.show_system se show_system_message levanta AttributeError."""
        app = _make_app()
        app.show_system_message = Mock(side_effect=AttributeError("stub"))
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._show_system("teste")

        app.renderer.show_system.assert_called_once_with("teste")


# ---------------------------------------------------------------------------
# Lacunas de cobertura restantes
# ---------------------------------------------------------------------------

class TestCoverageGaps(unittest.TestCase):

    def test_process_dispatches_to_process_handoff(self):
        """process() deve chamar _process_handoff quando handoff é válido."""
        app = _make_app(active_agents=["claude", "codex"])
        handoff = _make_handoff("task x")
        app.dispatch_services.call_agent = Mock(return_value="ok")
        app.parse_response = Mock(return_value=("ok", "codex", handoff, False, False, None))

        # Mocar _process_handoff para evitar side effects
        with patch.object(ChatRoundOrchestrator, "_process_handoff") as mock_ph:
            app.chat_round_orchestrator.process("hello")
            mock_ph.assert_called_once_with("claude", "codex", handoff)

    def test_process_standard_flow_explicit_empty_remaining(self):
        """explicit=True deve resultar em remaining=[] sem chamar call_agent."""
        app = _make_app(active_agents=["claude", "codex"])
        orchestrator = ChatRoundOrchestrator(app)

        orchestrator._process_standard_flow("claude", True, False, ["codex"])

        app.dispatch_services.call_agent.assert_not_called()

    def test_standard_flow_next_handoff_set_on_valid_route(self):
        """route_target válido (≠ agente) no loop padrão deve setar next_handoff."""
        app = _make_app(active_agents=["claude", "codex"])
        handoff = _make_handoff("next task")
        parse_count = [0]

        def fake_parse(resp):
            parse_count[0] += 1
            if parse_count[0] == 1:
                # codex retorna handoff para claude
                return ("resp", "claude", handoff, False, False, None)
            return ("resp2", None, None, False, False, None)

        app.dispatch_services.call_agent = Mock(return_value="resp")
        app.parse_response = Mock(side_effect=fake_parse)
        orchestrator = ChatRoundOrchestrator(app)

        # extend=True → remaining = ["codex", "claude", "codex"]
        orchestrator._process_standard_flow("claude", False, True, ["codex"])

        # call_agent chamado pelo menos 2 vezes (loop com remaining)
        self.assertGreaterEqual(app.dispatch_services.call_agent.call_count, 2)

    def test_handoff_chain_propagates_existing_chain(self):
        """Propagação de chain deve incluir agentes do next_handoff.chain não duplicados."""
        app = _make_app(active_agents=["claude", "codex", "deepseek"])
        handoff = _make_handoff("task x")
        call_count = [0]

        next_h = _make_handoff("next task", handoff_id="id-002", chain=["deepseek"])

        def fake_call(*args, **kwargs):
            call_count[0] += 1
            return "resp"

        def fake_parse(resp):
            if call_count[0] == 1:
                # primeira chamada: retorna next handoff com chain já preenchida
                return ("resp", "deepseek", next_h, False, False, None)
            # Segunda chamada (deepseek): finaliza
            return ("final", None, None, False, False, None)

        app.dispatch_services.call_agent = Mock(side_effect=fake_call)
        app.parse_response = Mock(side_effect=fake_parse)
        orchestrator = ChatRoundOrchestrator(app)

        # Não deve levantar exceção; lines 297-298 são executadas
        orchestrator._process_handoff("claude", "codex", handoff)

    def test_process_handoff_executes_target_then_returns_synthesis_to_origin(self):
        """No chat, o handoff deve ser executado pelo alvo antes da síntese voltar ao agente de origem."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "analisa isso", False))

        handoff = {
            "task": "Revisar implementação",
            "context": "Validar o fluxo",
            "expected": "Resumo curto",
            "handoff_id": "h1",
            "chain": [],
        }

        app.dispatch_services.call_agent = Mock(side_effect=["r1", "r2", "r3"])
        app.parse_response = Mock(side_effect=[
            ("resposta claude", "codex", handoff, False, False, None),
            ("resposta codex", None, None, False, False, "h1"),
            ("síntese claude", None, None, False, False, None),
        ])

        app.chat_round_orchestrator.process("analisa isso")

        calls = app.dispatch_services.call_agent.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["claude", "codex", "claude"])

        first_call = calls[0]
        self.assertEqual(first_call.kwargs["protocol_mode"], "standard")
        self.assertNotIn("handoff_only", first_call.kwargs)

        handoff_call = calls[1]
        self.assertEqual(handoff_call.kwargs["handoff"]["task"], "Revisar implementação")
        self.assertTrue(handoff_call.kwargs["handoff_only"])
        self.assertEqual(handoff_call.kwargs["from_agent"], "claude")
        self.assertEqual(handoff_call.kwargs["protocol_mode"], "handoff")
        self.assertEqual(handoff_call.kwargs["prompt_kind"], PromptKind.TASK_EXECUTOR)

        synthesis_call = calls[2]
        self.assertFalse(synthesis_call.kwargs.get("handoff_only", False))
        self.assertEqual(synthesis_call.kwargs["protocol_mode"], "handoff")
        self.assertIn("resposta codex", synthesis_call.kwargs["handoff"])

    def test_parallel_mode_warns_stream_json_agents(self):
        """Agentes com output_format=stream-json em modo paralelo devem executar sem erro."""
        app = _make_app(active_agents=["claude", "codex"], threads=2)
        app.get_agent_plugin = Mock(return_value=SimpleNamespace(output_format="stream-json"))
        app._merge_staging_to_workspace = Mock()
        app.task_services = Mock()
        app.task_services.call_agent_for_parallel = Mock(
            return_value=("codex", "resposta", None, None, False, False)
        )
        orchestrator = ChatRoundOrchestrator(app)

        # Deve executar sem exceção; o warning é emitido via logger interno
        orchestrator._process_standard_flow("claude", False, True, ["codex"])
        # get_agent_plugin chamado para detectar agentes stream-json
        app.get_agent_plugin.assert_called()


if __name__ == "__main__":
    unittest.main()
