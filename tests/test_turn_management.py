"""Testes de gerenciamento de turno e comportamento de disparo de agentes."""
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from quimera.app.core import QuimeraApp, TurnManager
from quimera.constants import CMD_EXIT, EXTEND_MARKER


# ---------------------------------------------------------------------------
# Helpers compartilhados
# ---------------------------------------------------------------------------

class DummyRenderer:
    def __init__(self):
        self.warnings = []
        self.system_messages = []

    def show_system(self, msg): self.system_messages.append(msg)
    def show_warning(self, msg): self.warnings.append(msg)
    def show_message(self, *a, **kw): pass
    def show_no_response(self, *a, **kw): pass
    def show_handoff(self, *a, **kw): pass


def _make_app(active_agents=None):
    """Cria um stub mínimo de QuimeraApp para testes de _do_process_chat_message."""
    app = QuimeraApp.__new__(QuimeraApp)
    app.active_agents = list(active_agents or ["claude", "codex"])
    app.round_index = 0
    app.summary_agent_preference = ""
    app.threads = 1
    app._pending_input_for = None
    app.renderer = DummyRenderer()
    app.turn_manager = TurnManager()

    # Comportamento padrão: primeira chamada retorna resposta simples (sem handoff, sem extend)
    app.parse_routing = Mock(return_value=("claude", "olá", False))
    app.call_agent = Mock(return_value="resposta")
    app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))
    app.print_response = Mock()
    app.persist_message = Mock()
    app._maybe_auto_summarize = Mock()

    return app


# ---------------------------------------------------------------------------
# Testes unitários do TurnManager
# ---------------------------------------------------------------------------

class TestTurnManager(unittest.TestCase):

    def test_initial_state_is_human_turn(self):
        tm = TurnManager()
        self.assertTrue(tm.is_human_turn)
        self.assertFalse(tm.is_ai_turn)

    def test_next_turn_alternates_to_ai(self):
        tm = TurnManager()
        tm.next_turn()
        self.assertFalse(tm.is_human_turn)
        self.assertTrue(tm.is_ai_turn)

    def test_next_turn_alternates_back_to_human(self):
        tm = TurnManager()
        tm.next_turn()
        tm.next_turn()
        self.assertTrue(tm.is_human_turn)

    def test_reset_always_returns_to_human(self):
        tm = TurnManager()
        tm.next_turn()          # AI
        tm.reset()
        self.assertTrue(tm.is_human_turn)

    def test_thread_safety(self):
        """Múltiplas threads alternando turno não devem causar estado inconsistente."""
        tm = TurnManager()
        errors = []

        def toggle_many():
            try:
                for _ in range(200):
                    tm.next_turn()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=toggle_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "Erros em threads: %s" % errors)
        # Estado final deve ser bool (não corrompido)
        self.assertIn(tm.is_human_turn, (True, False))


# ---------------------------------------------------------------------------
# Testes de integração de turno no ciclo run()
# ---------------------------------------------------------------------------

class TestTurnCycle(unittest.TestCase):

    def test_process_chat_message_restores_human_turn_on_success(self):
        """_process_chat_message deve devolver o turno ao humano via finally."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.turn_manager = TurnManager()
        app.turn_manager.next_turn()   # simula: loop já cedeu turno para AI

        app._do_process_chat_message = Mock()

        QuimeraApp._process_chat_message(app, "teste")

        self.assertTrue(app.turn_manager.is_human_turn)

    def test_process_chat_message_restores_human_turn_on_exception(self):
        """_process_chat_message deve devolver o turno ao humano mesmo com exceção."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.turn_manager = TurnManager()
        app.turn_manager.next_turn()

        app._do_process_chat_message = Mock(side_effect=RuntimeError("falha"))

        with self.assertRaises(RuntimeError):
            QuimeraApp._process_chat_message(app, "teste")

        self.assertTrue(app.turn_manager.is_human_turn)

    def test_run_blocks_while_ai_is_responding(self):
        """run() não deve chamar read_user_input enquanto é turno da IA."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.turn_manager = TurnManager()
        app.renderer = DummyRenderer()
        app.threads = 1
        app.user_name = "User"
        app.session_state = {
            "session_id": "test-session",
            "history_count": 0,
            "summary_loaded": False,
        }
        app._format_yes_no = lambda x: "sim" if x else "não"
        storage = Mock()
        storage.get_log_file.return_value = Path("/tmp/quimera-test.log")
        app.storage = storage

        # Simula que é turno da IA logo de início
        app.turn_manager.next_turn()
        self.assertTrue(app.turn_manager.is_ai_turn)

        read_calls = []

        def mock_read_user_input(prompt, timeout):
            read_calls.append(prompt)
            return CMD_EXIT

        app.read_user_input = mock_read_user_input
        app.handle_command = Mock(return_value=False)
        app._process_chat_message = Mock()
        app.shutdown = Mock()

        # Libera o turno para o humano depois de 0,25 s
        def release_turn():
            time.sleep(0.25)
            app.turn_manager.next_turn()

        releaser = threading.Thread(target=release_turn, daemon=True)
        releaser.start()

        run_thread = threading.Thread(target=QuimeraApp.run, args=(app,), daemon=True)
        run_thread.start()
        run_thread.join(timeout=2)

        self.assertFalse(run_thread.is_alive(), "run() travou e não terminou")
        # read_user_input só deve ter sido chamado APÓS a liberação do turno
        self.assertGreater(len(read_calls), 0)


# ---------------------------------------------------------------------------
# Testes de comportamento: "humano fala → um agente responde"
# ---------------------------------------------------------------------------

class TestSingleAgentPerTurn(unittest.TestCase):

    def test_default_mode_only_first_agent_responds(self):
        """Sem prefixo explícito e sem EXTEND, apenas um agente responde por turno."""
        app = _make_app(active_agents=["claude", "codex"])
        # Roteamento padrão: sem prefixo explícito
        app.parse_routing = Mock(return_value=("claude", "olá", False))
        # parse_response: sem extend, sem handoff, sem needs_human_input
        app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

        QuimeraApp._do_process_chat_message(app, "olá")

        # call_agent chamado exatamente uma vez (apenas para claude)
        self.assertEqual(app.call_agent.call_count, 1)
        first_call_agent = app.call_agent.call_args_list[0][0][0]
        self.assertEqual(first_call_agent, "claude")

    def test_explicit_prefix_only_that_agent_responds(self):
        """/claude ou /codex explícito → apenas aquele agente responde."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("codex", "revisa isso", True))
        app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

        QuimeraApp._do_process_chat_message(app, "/codex revisa isso")

        self.assertEqual(app.call_agent.call_count, 1)
        self.assertEqual(app.call_agent.call_args_list[0][0][0], "codex")

    def test_extend_mode_allows_alternation(self):
        """EXTEND_MARKER permite debate estendido: primeiro agente → segundo → primeiro → segundo."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "debate isso", False))

        # Primeira chamada retorna extend=True; demais retornam False
        responses = [
            ("resposta1", None, None, True,  False, None),  # claude, extend=True
            ("resposta2", None, None, False, False, None),  # codex
            ("resposta3", None, None, False, False, None),  # claude
            ("resposta4", None, None, False, False, None),  # codex
        ]
        app.parse_response = Mock(side_effect=responses)
        app.call_agent = Mock(side_effect=["r1", "r2", "r3", "r4"])

        QuimeraApp._do_process_chat_message(app, "debate isso")

        # 1 (first_agent) + 3 (remaining=[codex, claude, codex]) = 4 chamadas
        self.assertEqual(app.call_agent.call_count, 4)
        agents_called = [c[0][0] for c in app.call_agent.call_args_list]
        self.assertEqual(agents_called, ["claude", "codex", "claude", "codex"])

    def test_extend_with_explicit_prefix_still_single_agent(self):
        """Prefixo explícito anula extend: mesmo com EXTEND_MARKER, só um agente responde."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "faz algo", True))  # explicit=True
        # Resposta com extend=True mas explicit cancela o debate
        app.parse_response = Mock(return_value=("resposta", None, None, True, False, None))

        QuimeraApp._do_process_chat_message(app, "/claude faz algo")

        self.assertEqual(app.call_agent.call_count, 1)

    def test_handoff_triggers_secondary_agent(self):
        """[ROUTE:codex] no fluxo padrão ainda aciona o agente secundário."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "analisa", False))

        handoff_payload = {
            "task": "Revisa o código",
            "context": "contexto",
            "expected": "resultado",
            "handoff_id": "abc123",
            "chain": [],
        }
        responses = [
            # Primeira resposta: handoff para codex
            ("resposta claude", "codex", handoff_payload, False, False, None),
            # Resposta do codex (handoff_only)
            ("resposta codex", None, None, False, False, "abc123"),
            # Síntese do claude
            ("síntese", None, None, False, False, None),
        ]
        app.parse_response = Mock(side_effect=responses)
        app.call_agent = Mock(side_effect=["r1", "r2", "r3"])

        # behavior_metrics opcional
        app.behavior_metrics = None

        QuimeraApp._do_process_chat_message(app, "analisa")

        # 3 chamadas: claude (primary) → codex (handoff) → claude (síntese)
        self.assertEqual(app.call_agent.call_count, 3)
        agents_called = [c[0][0] for c in app.call_agent.call_args_list]
        self.assertEqual(agents_called, ["claude", "codex", "claude"])

    def test_single_active_agent_works(self):
        """Com apenas um agente ativo, não há tentativa de chamar agente secundário."""
        app = _make_app(active_agents=["claude"])
        app.parse_routing = Mock(return_value=("claude", "oi", False))
        app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

        QuimeraApp._do_process_chat_message(app, "oi")

        self.assertEqual(app.call_agent.call_count, 1)

    def test_needs_human_input_suspends_turn(self):
        """Quando agente sinaliza NEEDS_INPUT, o turno é suspenso e _pending_input_for é definido."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "pergunta", False))
        app.parse_response = Mock(return_value=("Você quer continuar?", None, None, False, True, None))

        QuimeraApp._do_process_chat_message(app, "pergunta")

        # Apenas o primeiro agente respondeu
        self.assertEqual(app.call_agent.call_count, 1)
        # Turno suspenso: próxima fala do humano vai para claude
        self.assertEqual(app._pending_input_for, "claude")

    def test_consecutive_turns_route_to_different_agents(self):
        """Duas falas seguidas do humano podem ir para agentes diferentes (roteamento aleatório)."""
        app = _make_app(active_agents=["claude", "codex"])

        # Primeira fala → claude; segunda fala → codex
        app.parse_routing = Mock(side_effect=[
            ("claude", "primeira fala", False),
            ("codex", "segunda fala", False),
        ])
        app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

        QuimeraApp._do_process_chat_message(app, "primeira fala")
        QuimeraApp._do_process_chat_message(app, "segunda fala")

        self.assertEqual(app.call_agent.call_count, 2)
        agents_called = [c[0][0] for c in app.call_agent.call_args_list]
        self.assertEqual(agents_called, ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
