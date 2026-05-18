"""Testes de gerenciamento de turno e comportamento de disparo de agentes."""
import queue
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from quimera.app.agent_pool import AgentPool
from quimera.app.chat_round import ChatRoundOrchestrator
from quimera.app.core import QuimeraApp, TurnManager
from quimera.app.render_event import RenderEvent
from quimera.app.worker import ChatWorker
from quimera.constants import CMD_EXIT


# ---------------------------------------------------------------------------
# Helpers compartilhados
# ---------------------------------------------------------------------------

class DummyRenderer:
    def __init__(self):
        self.warnings = []
        self.system_messages = []
        self.errors = []

    def show_system(self, msg): self.system_messages.append(msg)

    def show_warning(self, msg): self.warnings.append(msg)

    def show_error(self, msg): self.errors.append(msg)

    def show_message(self, *a, **kw): pass

    def show_no_response(self, *a, **kw): pass

    def show_handoff(self, *a, **kw): pass


def _make_app(active_agents=None):
    """Cria um stub mínimo de QuimeraApp para testes de _do_process_chat_message."""
    agents = list(active_agents or ["claude", "codex"])
    app = QuimeraApp.__new__(QuimeraApp)
    app.agent_pool = AgentPool(agents)
    app._round_index_val = 0
    app._summary_agent_preference_val = ""
    app.threads = 1
    app._pending_input_for_val = None
    app.renderer = DummyRenderer()
    app.turn_manager = TurnManager()

    # Comportamento padrão: primeira chamada retorna resposta simples (sem handoff, sem extend)
    app.parse_routing = Mock(return_value=("claude", "olá", False))
    app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))
    app.session_services = Mock()
    app.dispatch_services = Mock()
    app.dispatch_services.call_agent = Mock(return_value="resposta")
    app.dispatch_services.print_response = Mock()
    app.chat_round_orchestrator = ChatRoundOrchestrator(
        dispatch_services=app.dispatch_services,
        parse_routing=app.parse_routing,
        agent_pool=app.agent_pool,
        session_services=app.session_services,
        parse_response=app.parse_response,
        agent_client=Mock(_user_cancelled=False),
        turn_manager=app.turn_manager,
        threads=app.threads,
        renderer=app.renderer,
        get_round_index=lambda: app._round_index_val,
        set_round_index=lambda v: setattr(app, '_round_index_val', v),
        set_summary_agent_preference=lambda v: setattr(app, '_summary_agent_preference_val', v),
        get_pending_input_for=lambda: app._pending_input_for_val,
        set_pending_input_for=lambda v: setattr(app, '_pending_input_for_val', v),
        generate_handoff_id=lambda task, target: f"gen-{target}",
    )
    app._generate_handoff_id = lambda task, target: f"gen-{target}"

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
        tm.next_turn()  # AI
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
        app.turn_manager.next_turn()  # simula: loop já cedeu turno para AI

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
        app.session_services = Mock()
        app.agent_client = Mock()

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

    def test_run_does_not_block_on_keyboard_interrupt_while_chat_worker_is_busy(self):
        """run() deve encerrar mesmo se o worker do chat estiver preso processando."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.threads = 2
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
        app.handle_command = Mock(return_value=False)
        app.session_services = Mock()
        app.agent_client = Mock()
        app.turn_manager = TurnManager()
        app._build_input_prompt = lambda: "User: "
        reads = iter(["mensagem", CMD_EXIT])
        app.read_user_input = lambda prompt, timeout: next(reads)

        def slow_process(_user):
            time.sleep(10)

        app._process_chat_message = slow_process

        def interrupting_wait(timeout=None):
            raise KeyboardInterrupt()

        app.turn_manager.wait_for_human_turn = interrupting_wait

        run_thread = threading.Thread(target=QuimeraApp.run, args=(app,), daemon=True)
        run_thread.start()
        run_thread.join(timeout=2)

        self.assertFalse(run_thread.is_alive(), "run() travou no encerramento com worker ocupado")
        app.session_services.shutdown.assert_called_once()

    def test_process_chat_message_keeps_ai_turn_while_async_queue_has_pending_work(self):
        """Um prompt concluído não deve devolver o turno se ainda houver trabalho assíncrono pendente."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.turn_manager = TurnManager()
        app.turn_manager.next_turn()
        app._chat_inflight_count = 2
        app._chat_inflight_lock = threading.Lock()
        app.agent_client = Mock(_user_cancelled=False, _cancel_event=None)
        app._do_process_chat_message = Mock()

        QuimeraApp._process_chat_message(app, "mensagem")

        self.assertTrue(app.turn_manager.is_ai_turn)

    def test_run_falls_back_to_sync_when_chat_worker_dies(self):
        """run() deve alertar e voltar ao modo síncrono quando o worker morre."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.threads = 2
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
        app.handle_command = Mock(return_value=False)
        app.session_services = Mock()
        app.agent_client = Mock()
        app.show_error_message = QuimeraApp.show_error_message.__get__(app, QuimeraApp)
        app._build_input_prompt = lambda: "User: "
        processed = []
        read_values = iter(["mensagem", CMD_EXIT])

        def mock_read_user_input(prompt, timeout):
            return next(read_values)

        app.read_user_input = mock_read_user_input

        def record_message(user):
            processed.append(user)
            app.turn_manager.reset()

        app._process_chat_message = record_message

        class DeadWorker:
            def start(self):
                return None

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        with patch("quimera.app.core.ChatWorker", return_value=DeadWorker()):
            QuimeraApp.run(app)

        self.assertEqual(processed, ["mensagem"])
        self.assertEqual(len(app.renderer.errors), 1)
        self.assertIn("worker do chat interrompido", app.renderer.errors[0])

    def test_run_blocks_until_slot_frees_then_processes_prompt_sync(self):
        """Com todos os slots ocupados, o próximo prompt espera e roda no thread principal."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.threads = 2
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
        app.handle_command = Mock(return_value=False)
        app.session_services = Mock()
        app.agent_client = Mock()
        app.turn_manager = TurnManager()
        app._chat_inflight_count = 0
        app._chat_inflight_lock = threading.Lock()
        app._build_input_prompt = lambda: "User: "
        reads = iter(["m1", "m2", "m3", CMD_EXIT])
        app.read_user_input = lambda prompt, timeout: next(reads)
        calls = []

        def observed_process(user):
            mode = "sync" if threading.current_thread() is threading.main_thread() else "async"
            calls.append((user, mode, time.monotonic()))
            if user in {"m1", "m2"}:
                time.sleep(0.25)

        app._process_chat_message = observed_process

        started = time.monotonic()
        QuimeraApp.run(app)
        elapsed = time.monotonic() - started

        self.assertEqual([user for user, _, _ in calls], ["m1", "m2", "m3"])
        self.assertEqual([mode for _, mode, _ in calls[:2]], ["async", "async"])
        self.assertEqual(calls[2][1], "sync")
        self.assertGreaterEqual(elapsed, 0.20)

    def test_threads_one_is_serial(self):
        """Com threads=1, todos os prompts rodam serialmente no thread principal."""
        app = QuimeraApp.__new__(QuimeraApp)
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
        app.handle_command = Mock(return_value=False)
        app.session_services = Mock()
        app.agent_client = Mock()
        app.turn_manager = TurnManager()
        app._build_input_prompt = lambda: "User: "
        reads = iter(["m1", "m2", CMD_EXIT])
        app.read_user_input = lambda prompt, timeout: next(reads)
        calls = []

        def observed_process(user):
            calls.append((user, threading.current_thread() is threading.main_thread()))

        app._process_chat_message = observed_process

        QuimeraApp.run(app)

        self.assertEqual(calls, [("m1", True), ("m2", True)])

    def test_run_enqueues_before_switching_turn_with_worker(self):
        """No modo com worker, run() deve enfileirar a mensagem antes de ceder o turno."""
        app = QuimeraApp.__new__(QuimeraApp)
        app.renderer = DummyRenderer()
        app.threads = 2
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
        app.handle_command = Mock(return_value=False)
        app.session_services = Mock()
        app.agent_client = Mock()
        app._build_input_prompt = lambda: "User: "
        order = []
        real_turn_manager = TurnManager()

        class ObservedTurnManager:
            @property
            def is_human_turn(self):
                return real_turn_manager.is_human_turn

            def wait_for_human_turn(self, timeout=None):
                return real_turn_manager.wait_for_human_turn(timeout=timeout)

            def next_turn(self):
                order.append("next_turn")
                return real_turn_manager.next_turn()

            def reset(self):
                return real_turn_manager.reset()

        app.turn_manager = ObservedTurnManager()
        read_values = iter(["mensagem", CMD_EXIT])

        def mock_read_user_input(prompt, timeout):
            return next(read_values)

        app.read_user_input = mock_read_user_input
        app._process_chat_message = Mock()

        class ObservedQueue(queue.Queue):
            def put(self, item, block=True, timeout=None):
                if item not in (None,):
                    order.append(f"put:{item}")
                return super().put(item, block=block, timeout=timeout)

        original_queue_cls = queue.Queue

        def queue_factory(*args, **kwargs):
            if not hasattr(queue_factory, "calls"):
                queue_factory.calls = 0
            queue_factory.calls += 1
            if queue_factory.calls == 2:
                return ObservedQueue()
            return original_queue_cls(*args, **kwargs)

        class IdleWorker:
            def __init__(self):
                self._checks = 0

            def start(self):
                return None

            def is_alive(self):
                self._checks += 1
                return self._checks == 1

            def join(self, timeout=None):
                return None

        with patch("quimera.app.core.queue.Queue", side_effect=queue_factory), patch(
            "quimera.app.core.ChatWorker", return_value=IdleWorker()
        ):
            QuimeraApp.run(app)

        self.assertEqual(order[:2], ["put:mensagem", "next_turn"])


class TestChatWorker(unittest.TestCase):

    def test_process_chat_queue_keeps_worker_alive_after_exception(self):
        """Erros do executor devem ser capturados sem matar o fluxo do worker."""
        turn_manager = Mock()
        ui_queue = queue.Queue()
        worker = ChatWorker(
            chat_queue=queue.Queue(),
            ui_event_queue=ui_queue,
            agent_executor=Mock(side_effect=RuntimeError("boom")),
            turn_manager=turn_manager,
        )

        worker._process_chat_queue("mensagem")

        event = ui_queue.get_nowait()
        self.assertEqual(event.type, RenderEvent.ERROR)
        self.assertEqual(event.payload, "boom")
        turn_manager.reset.assert_called_once()


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
        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)
        first_call_agent = app.dispatch_services.call_agent.call_args_list[0][0][0]
        self.assertEqual(first_call_agent, "claude")

    def test_explicit_prefix_only_that_agent_responds(self):
        """/claude ou /codex explícito → apenas aquele agente responde."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("codex", "revisa isso", True))
        app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

        QuimeraApp._do_process_chat_message(app, "/codex revisa isso")

        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)
        self.assertEqual(app.dispatch_services.call_agent.call_args_list[0][0][0], "codex")

    def test_extend_mode_stays_on_first_agent(self):
        """EXTEND_MARKER no chat interativo não deve mais disparar outros agentes no mesmo prompt."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "debate isso", False))
        app.parse_response = Mock(return_value=("resposta1", None, None, True, False, None))
        app.dispatch_services.call_agent = Mock(return_value="r1")

        QuimeraApp._do_process_chat_message(app, "debate isso")

        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)
        self.assertEqual(app.dispatch_services.call_agent.call_args_list[0][0][0], "claude")

    def test_extend_with_explicit_prefix_still_single_agent(self):
        """Prefixo explícito anula extend: mesmo com EXTEND_MARKER, só um agente responde."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "faz algo", True))  # explicit=True
        # Resposta com extend=True mas explicit cancela o debate
        app.parse_response = Mock(return_value=("resposta", None, None, True, False, None))

        QuimeraApp._do_process_chat_message(app, "/claude faz algo")

        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)

    def test_handoff_triggers_secondary_agent(self):
        """Handoff JSON no fluxo padrão ainda aciona o agente secundário."""
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
        app.dispatch_services.call_agent = Mock(side_effect=["r1", "r2", "r3"])

        # behavior_metrics opcional
        app.behavior_metrics = None

        QuimeraApp._do_process_chat_message(app, "analisa")

        # 3 chamadas: claude (primary) → codex (handoff) → claude (síntese)
        self.assertEqual(app.dispatch_services.call_agent.call_count, 3)
        agents_called = [c[0][0] for c in app.dispatch_services.call_agent.call_args_list]
        self.assertEqual(agents_called, ["claude", "codex", "claude"])

    def test_self_handoff_is_ignored(self):
        """Handoff de um agente para si mesmo deve ser ignorado — não gera nova chamada."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "analisa", False))

        handoff_payload = {
            "task": "Revisa o código",
            "context": "contexto",
            "expected": "resultado",
            "handoff_id": "abc123",
            "chain": [],
        }
        # parse_response retorna route_target == first_agent ("claude") — self-handoff
        app.parse_response = Mock(return_value=("resposta claude", "claude", handoff_payload, False, False, None))
        app.dispatch_services.call_agent = Mock(return_value="resposta claude")

        QuimeraApp._do_process_chat_message(app, "analisa")

        # Apenas 1 chamada (claude como primary) — sem chamada extra para self-handoff
        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)

    def test_handoff_secondary_can_delegate_to_third_before_synthesis(self):
        """Se o secundário delega de novo, o terceiro responde em handoff_only antes da síntese."""
        app = _make_app(active_agents=["claude", "codex", "opencode-qwen"])
        app.parse_routing = Mock(return_value=("claude", "analisa", False))

        first_handoff = {
            "task": "Revisa parser",
            "context": "Validar regras",
            "expected": "2 bullets",
            "handoff_id": "h1",
            "chain": [],
        }
        second_handoff = {
            "task": "Valida edge cases",
            "context": "Cobrir entradas inválidas",
            "expected": "1 resumo curto",
            "handoff_id": "h2",
            "chain": ["claude"],
        }
        app.parse_response = Mock(side_effect=[
            ("resposta claude", "codex", first_handoff, False, False, None),
            ("resposta codex", "opencode-qwen", second_handoff, False, False, "h1"),
            ("resposta qwen", None, None, False, False, "h2"),
            ("síntese final", None, None, False, False, None),
        ])
        app.dispatch_services.call_agent = Mock(side_effect=["r1", "r2", "r3", "r4"])

        QuimeraApp._do_process_chat_message(app, "analisa")

        self.assertEqual(app.dispatch_services.call_agent.call_count, 4)
        calls = app.dispatch_services.call_agent.call_args_list
        self.assertEqual([c[0][0] for c in calls], ["claude", "codex", "opencode-qwen", "claude"])

        self.assertTrue(calls[1][1]["handoff_only"])
        self.assertEqual(calls[1][1]["from_agent"], "claude")
        self.assertTrue(calls[2][1]["handoff_only"])
        self.assertEqual(calls[2][1]["handoff"]["task"], "Valida edge cases")
        self.assertIn("resposta qwen", calls[3][1]["handoff"])

    def test_handoff_circular_chain_does_not_loop(self):
        """Tentativa de ciclo em handoff não deve entrar em loop infinito."""
        app = _make_app(active_agents=["claude", "codex", "opencode-qwen"])
        app.parse_routing = Mock(return_value=("claude", "analisa", False))

        first_handoff = {
            "task": "Revisa parser",
            "context": "Validar regras",
            "expected": "2 bullets",
            "handoff_id": "h1",
            "chain": [],
        }
        circular_handoff = {
            "task": "Volta para claude",
            "context": "Confere etapa anterior",
            "expected": "1 linha",
            "handoff_id": "h2",
            "chain": ["claude", "codex"],
        }
        parsed = iter([
            ("resposta claude", "codex", first_handoff, False, False, None),
            ("resposta codex", "claude", circular_handoff, False, False, "h1"),
            ("síntese", None, None, False, False, None),
        ])

        def fake_parse_response(_response):
            try:
                return next(parsed)
            except StopIteration:
                return "extra", None, None, False, False, None

        calls = []

        def fake_call_agent(agent, *args, **kwargs):
            calls.append((agent, kwargs))
            if len(calls) > 6:
                raise AssertionError("Loop detectado: call_agent excedeu limite esperado")
            return f"r{len(calls)}"

        app.parse_response = Mock(side_effect=fake_parse_response)
        app.dispatch_services.call_agent = Mock(side_effect=fake_call_agent)

        QuimeraApp._do_process_chat_message(app, "analisa")

        self.assertLessEqual(app.dispatch_services.call_agent.call_count, 4)
        handoff_only_calls = [call for call in calls if call[1].get("handoff_only")]
        self.assertLessEqual(len(handoff_only_calls), 2)

    def test_single_active_agent_works(self):
        """Com apenas um agente ativo, não há tentativa de chamar agente secundário."""
        app = _make_app(active_agents=["claude"])
        app.parse_routing = Mock(return_value=("claude", "oi", False))
        app.parse_response = Mock(return_value=("resposta", None, None, False, False, None))

        QuimeraApp._do_process_chat_message(app, "oi")

        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)

    def test_needs_human_input_suspends_turn(self):
        """Quando agente sinaliza NEEDS_INPUT, o turno é suspenso e _pending_input_for é definido."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "pergunta", False))
        app.parse_response = Mock(return_value=("Você quer continuar?", None, None, False, True, None))

        QuimeraApp._do_process_chat_message(app, "pergunta")

        # Apenas o primeiro agente respondeu
        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)
        # Turno suspenso: próxima fala do humano vai para claude
        self.assertEqual(app._pending_input_for_val, "claude")

    def test_needs_human_input_uses_prompt_aware_system_message_when_available(self):
        """Quando disponível, usa show_system_message para evitar saída inline com o prompt."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "pergunta", False))
        app.parse_response = Mock(return_value=("Você quer continuar?", None, None, False, True, None))
        app.show_system_message = Mock()

        QuimeraApp._do_process_chat_message(app, "pergunta")

        app.show_system_message.assert_called_once_with("\nResponda para CLAUDE:\n")

    def test_handoff_without_body_continues_chain(self):
        """Agente que responde só com handoff JSON (sem body) não cai em fallback."""
        app = _make_app(active_agents=["claude", "codex", "opencode-qwen"])
        app.parse_routing = Mock(return_value=("claude", "analisa", False))

        first_handoff = {
            "task": "Revisa código",
            "context": "branch main",
            "expected": "lista de issues",
            "handoff_id": "h1",
            "chain": [],
        }
        second_handoff = {
            "task": "Executa correções",
            "context": "issues listados",
            "expected": "patch aplicado",
            "handoff_id": "h2",
            "chain": ["claude"],
        }
        # codex responde com apenas handoff (body=None) — sem texto, só delegação
        app.parse_response = Mock(side_effect=[
            ("resposta claude", "codex", first_handoff, False, False, None),
            (None, "opencode-qwen", second_handoff, False, False, "h1"),  # sem body
            ("resposta qwen", None, None, False, False, "h2"),
            ("síntese final", None, None, False, False, None),
        ])
        app.dispatch_services.call_agent = Mock(side_effect=["r1", "r2", "r3", "r4"])

        QuimeraApp._do_process_chat_message(app, "analisa")

        calls = app.dispatch_services.call_agent.call_args_list
        agents_called = [c[0][0] for c in calls]
        # Deve chamar: claude → codex → opencode-qwen → claude (síntese)
        self.assertEqual(agents_called, ["claude", "codex", "opencode-qwen", "claude"])
        # Não deve ter tentado fallback (apenas 4 chamadas esperadas)
        self.assertEqual(app.dispatch_services.call_agent.call_count, 4)

    def test_handoff_to_unknown_agent_is_ignored(self):
        """Handoff para agente não conectado deve ser ignorado silenciosamente."""
        app = _make_app(active_agents=["claude", "codex"])
        app.parse_routing = Mock(return_value=("claude", "mensagem", False))

        handoff = {
            "task": "alguma tarefa",
            "context": "",
            "expected": "",
            "handoff_id": "hx",
            "chain": [],
        }
        # claude responde com handoff para agente fora de active_agents
        app.parse_response = Mock(return_value=("resposta claude", "agente", handoff, False, False, None))

        QuimeraApp._do_process_chat_message(app, "mensagem")

        # Apenas claude deve ter sido chamado; handoff para 'agente' ignorado
        self.assertEqual(app.dispatch_services.call_agent.call_count, 1)
        agents_called = [c[0][0] for c in app.dispatch_services.call_agent.call_args_list]
        self.assertEqual(agents_called, ["claude"])

    def test_sequential_handoffs_accumulate_all_delegate_responses_for_synthesis(self):
        """handoffs em sequência devem sintetizar com o resultado de todos os agentes delegados."""
        app = _make_app(active_agents=["claude", "codex", "opencode-qwen"])
        app.parse_routing = Mock(return_value=("claude", "analisa", False))

        first_handoff = {
            "task": "Revisar implementação",
            "context": "contexto",
            "expected": "resumo",
            "handoff_id": "h1",
            "chain": [],
            "_pending_handoffs": [
                {
                    "route": "opencode-qwen",
                    "content": "Validar edge cases",
                    "metadata": {
                        "context": "contexto 2",
                        "expected": "resumo 2",
                    },
                    "handoff_id": "h2",
                }
            ],
        }
        app.parse_response = Mock(side_effect=[
            (None, "codex", first_handoff, False, False, None),
            ("resposta codex", None, None, False, False, "h1"),
            ("resposta qwen", None, None, False, False, "h2"),
            ("síntese final", None, None, False, False, None),
        ])
        app.dispatch_services.call_agent = Mock(side_effect=["r1", "r2", "r3", "r4"])

        QuimeraApp._do_process_chat_message(app, "analisa")

        calls = app.dispatch_services.call_agent.call_args_list
        self.assertEqual([c[0][0] for c in calls], ["claude", "codex", "opencode-qwen", "claude"])
        synthesis_handoff = calls[3][1]["handoff"]
        self.assertIn("CODEX:\nresposta codex", synthesis_handoff)
        self.assertIn("OPENCODE-QWEN:\nresposta qwen", synthesis_handoff)

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

        self.assertEqual(app.dispatch_services.call_agent.call_count, 2)
        agents_called = [c[0][0] for c in app.dispatch_services.call_agent.call_args_list]
        self.assertEqual(agents_called, ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
