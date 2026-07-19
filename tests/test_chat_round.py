"""Testes de cobertura para quimera/app/chat_round.py."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from quimera.app.agent_pool import AgentPool
from quimera.app.chat_round import ChatRoundOrchestrator
from quimera.app.staging import merge_staging_to_workspace
from quimera.domain.session_state import SessionRuntimeState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(active_agents=None, threads=1):
    """Cria um app mínimo para testes do ChatRoundOrchestrator."""
    app = SimpleNamespace()
    app.agent_pool = AgentPool(list(active_agents or ["claude", "codex"]))
    app.threads = threads
    app.round_index = 0
    app.summary_agent_preference = None
    app.session_state = SessionRuntimeState()
    app.session_state["session_id"] = "test-cr"
    app.session_state["history_count"] = 0
    app.shared_state = {}
    app.behavior_metrics = None
    app._parallel_toolbar_state = {
        "active": 0,
        "queued": 0,
        "capacity": max(0, threads),
        "active_agents": (),
    }
    app._parallel_toolbar_updates = []

    app.renderer = Mock()
    app.renderer.show_warning = Mock()
    app.renderer.show_system = Mock()
    app.renderer.show_delegation = Mock()

    app.dispatch_services = Mock()
    app.dispatch_services.delegate = Mock(return_value=None)
    app.dispatch_services.print_response = Mock()

    app.session_services = Mock()
    app.session_services.persist_message = Mock()
    app.session_services.maybe_auto_summarize = Mock()
    app.task_services = Mock()
    app.task_services.delegate_for_parallel = Mock(
        return_value=("codex", "resposta", False, False)
    )

    app.turn_manager = Mock()
    app.turn_manager.reset = Mock()
    app.agent_client = Mock()
    app.agent_client._user_cancelled = False

    def _set_parallel_toolbar_state(**kwargs):
        app._parallel_toolbar_state.update(kwargs)
        app._parallel_toolbar_updates.append(dict(app._parallel_toolbar_state))

    app._set_parallel_toolbar_state = _set_parallel_toolbar_state

    # parse_routing padrão: ("claude", "hello", False)
    app.parse_routing = Mock(return_value=("claude", "hello", False))
    # parse_response padrão: resposta simples sem delegation
    app.parse_response = Mock(return_value=("resposta", None, None, None))

    app.chat_round_orchestrator = _make_orchestrator(app)
    return app


def _make_orchestrator(app):
    return ChatRoundOrchestrator(
        dispatch_services=app.dispatch_services,
        parse_routing=lambda user: app.parse_routing(user),
        agent_pool=app.agent_pool,
        session_services=app.session_services,
        parse_response=lambda resp: app.parse_response(resp),
        agent_client=getattr(app, 'agent_client', None),
        turn_manager=getattr(app, 'turn_manager', None),
        task_services=getattr(app, 'task_services', None),
        get_agent_profile=getattr(app, 'get_agent_profile', None),
        behavior_metrics=getattr(app, 'behavior_metrics', None),
        threads=getattr(app, 'threads', 1),
        session_state=app.session_state,
        renderer=getattr(app, 'renderer', None),
        show_system_message=(
            getattr(app, 'show_system_message', None)
            or getattr(getattr(app, 'system_layer', None), 'show_system_message', None)
        ),
        set_parallel_toolbar_state=getattr(app, '_set_parallel_toolbar_state', None),
        merge_staging_to_workspace=merge_staging_to_workspace,
    )



# ---------------------------------------------------------------------------
# process() — fluxo principal
# ---------------------------------------------------------------------------

class TestProcessMainFlow(unittest.TestCase):

    def test_parse_routing_none_returns_early(self):
        """Se parse_routing retorna None como agente, process() deve retornar sem chamar delegate."""
        app = _make_app()
        app.parse_routing = Mock(return_value=(None, None, None))
        app.chat_round_orchestrator.process("oi")
        app.dispatch_services.delegate.assert_not_called()

    def test_empty_message_shows_warning(self):
        """Mensagem vazia deve chamar show_warning."""
        app = _make_app()
        app.parse_routing = Mock(return_value=("claude", "   ", False))
        app.chat_round_orchestrator.process("   ")
        app.renderer.show_warning.assert_called_once()
        app.dispatch_services.delegate.assert_not_called()

    def test_user_cancelled_after_first_call(self):
        """_user_cancelled após primeira chamada deve resetar turno e retornar."""
        app = _make_app()
        app.dispatch_services.delegate = Mock(return_value="alguma resposta")
        app.parse_response = Mock(return_value=("alguma resposta", None, None, None))
        app.agent_client._user_cancelled = True

        app.chat_round_orchestrator.process("hello")

        app.turn_manager.reset.assert_called_once()
        # maybe_auto_summarize não deve ter sido chamado (retornou cedo)
        app.session_services.maybe_auto_summarize.assert_not_called()

    def test_fallback_skips_none_response(self):
        """Fallback deve continuar ao próximo candidato quando também retorna None."""
        app = _make_app(active_agents=["claude", "codex", "deepseek"])
        responses = iter([None, None, "deepseek respondeu"])
        app.dispatch_services.delegate = Mock(side_effect=lambda *a, **kw: next(responses))

        def fake_parse(resp):
            if resp is None:
                return (None, None, None, None)
            return (resp, None, None, None)

        app.parse_response = Mock(side_effect=fake_parse)

        app.chat_round_orchestrator.process("hello")

        # Chamado: claude (None), codex (None), deepseek (ok)
        self.assertEqual(app.dispatch_services.delegate.call_count, 3)

    def test_process_does_not_dispatch_other_agents_for_same_prompt(self):
        """Uma mensagem sem delegation aciona apenas o agente reservado para o prompt."""
        app = _make_app(active_agents=["codex", "claude", "opencode"], threads=2)
        app.parse_routing = Mock(return_value=("codex", "status", False))
        app.dispatch_services.delegate = Mock(return_value="resposta codex")
        app.parse_response = Mock(return_value=("resposta codex", None, None, None))

        app.chat_round_orchestrator.process("status")

        app.dispatch_services.delegate.assert_called_once_with(
            "codex",
            is_first_speaker=True,
            protocol_mode="standard",
            request_override="status",
            max_retries=1,
        )
        app.task_services.delegate_for_parallel.assert_not_called()
        self.assertEqual(
            [call.args for call in app.dispatch_services.print_response.call_args_list],
            [("codex", "resposta codex")],
        )

    def test_threaded_mode_failover_tries_next_agent(self):
        """Em threads>1, no-response deve tentar fallback para outros agentes."""
        app = _make_app(active_agents=["codex", "claude", "opencode"], threads=2)
        app.parse_routing = Mock(return_value=("codex", "status", False))
        responses = iter([None, "ok"])
        app.dispatch_services.delegate = Mock(side_effect=lambda *a, **kw: next(responses))

        def fake_parse(resp):
            if resp is None:
                return (None, None, None, None)
            return (resp, None, None, None)

        app.parse_response = Mock(side_effect=fake_parse)

        app.chat_round_orchestrator.process("status")

        self.assertEqual(app.dispatch_services.delegate.call_count, 2)
        self.assertEqual(
            app.dispatch_services.delegate.call_args_list[0].args[0],
            "codex",
        )
        self.assertEqual(
            app.dispatch_services.delegate.call_args_list[1].args[0],
            "claude",
        )

    def test_threaded_mode_all_agents_fail_ends_round(self):
        """Em threads>1, quando todos os agentes falham, a rodada termina."""
        app = _make_app(active_agents=["codex", "claude", "opencode"], threads=2)
        app.parse_routing = Mock(return_value=("codex", "status", False))
        app.dispatch_services.delegate = Mock(return_value=None)
        app.parse_response = Mock(return_value=(None, None, None, None))

        app.chat_round_orchestrator.process("status")

        self.assertEqual(app.dispatch_services.delegate.call_count, 3)
        app.renderer.show_warning.assert_called_once_with(
            "Nenhum agente disponível respondeu."
        )

    def test_main_flow_fallback_cancelled_resets_turn_without_warning(self):
        """Cancelamento após fallback no fluxo principal deve resetar turno e retornar."""
        app = _make_app(active_agents=["codex", "claude", "opencode"], threads=2)
        app.parse_routing = Mock(return_value=("codex", "status", False))
        call_count = [0]

        def fake_call(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            app.agent_client._user_cancelled = True
            return "claude respondeu"

        app.dispatch_services.delegate = Mock(side_effect=fake_call)

        def fake_parse(resp):
            if resp is None:
                return (None, None, None, None)
            return (resp, None, None, None)

        app.parse_response = Mock(side_effect=fake_parse)

        app.chat_round_orchestrator.process("status")

        self.assertEqual(app.dispatch_services.delegate.call_count, 2)
        app.turn_manager.reset.assert_called_once()
        app.renderer.show_warning.assert_not_called()
        app.dispatch_services.print_response.assert_not_called()

    def test_handle_cancelled_is_idempotent_for_shared_cancel(self):
        """Mesmo cancelamento compartilhado não deve chamar turn_manager.reset mais de uma vez."""
        app = _make_app(active_agents=["codex", "claude"], threads=2)
        orchestrator = app.chat_round_orchestrator

        orchestrator._handle_cancelled()
        orchestrator._handle_cancelled()

        app.renderer.show_system.assert_not_called()
        app.turn_manager.reset.assert_called_once()

    def test_main_flow_failover_message_uses_latest_failed_agent(self):
        """Mensagem de failover deve apontar o último agente que falhou."""
        app = _make_app(active_agents=["codex", "claude", "opencode"], threads=2)
        app.parse_routing = Mock(return_value=("codex", "status", False))
        responses = iter([None, None, "ok"])
        app.dispatch_services.delegate = Mock(side_effect=lambda *a, **kw: next(responses))

        def fake_parse(resp):
            if resp is None:
                return (None, None, None, None)
            return (resp, None, None, None)

        app.parse_response = Mock(side_effect=fake_parse)

        app.chat_round_orchestrator.process("status")

        failovers = [
            (call.args[0] if call.args else call.kwargs.get("agent"), call.kwargs.get("target"))
            for call in app.renderer.notify_agent_failover.call_args_list
        ]
        self.assertIn(("codex", "claude"), failovers)
        self.assertIn(("claude", "opencode"), failovers)

    def test_main_flow_cancelled_before_fallback_message_skips_fallback_notice(self):
        """Se cancelar no limiar do fallback, não deve imprimir aviso de fallback."""
        app = _make_app(active_agents=["codex", "claude", "opencode"], threads=2)
        app.parse_routing = Mock(return_value=("codex", "status", False))
        app.dispatch_services.delegate = Mock(return_value=None)
        app.parse_response = Mock(return_value=(None, None, None, None))

        cancel_checks = iter([False, False, False, True])
        app.chat_round_orchestrator._is_cancelled = Mock(side_effect=lambda: next(cancel_checks))

        app.chat_round_orchestrator.process("status")

        shown_messages = [call.args[0] for call in app.renderer.show_system.call_args_list if call.args]
        self.assertFalse(any(msg.startswith("[fallback]") for msg in shown_messages))
        app.turn_manager.reset.assert_called_once()

    def test_process_rotates_round_robin_at_prompt_start(self):
        """Prompts sequenciais sem prefixo explícito reservam agentes em round-robin."""
        app = _make_app(active_agents=["A", "B", "C"], threads=3)
        app.parse_routing = Mock(side_effect=[
            ("A", "p1", False),
            ("A", "p2", False),
            ("A", "p3", False),
        ])
        app.dispatch_services.delegate = Mock(side_effect=["r1", "r2", "r3"])
        app.parse_response = Mock(return_value=("ok", None, None, None))

        app.chat_round_orchestrator.process("p1")
        app.chat_round_orchestrator.process("p2")
        app.chat_round_orchestrator.process("p3")

        self.assertEqual(
            [call.args[0] for call in app.dispatch_services.delegate.call_args_list],
            ["A", "B", "C"],
        )


# ---------------------------------------------------------------------------
# _show_system helper
# ---------------------------------------------------------------------------

class TestShowSystem(unittest.TestCase):

    def test_show_system_uses_show_system_message_when_available(self):
        """_show_system deve preferir app.show_system_message se disponível."""
        app = _make_app()
        app.show_system_message = Mock()
        orchestrator = _make_orchestrator(app)

        orchestrator._show_system("teste")

        app.show_system_message.assert_called_once_with("teste")
        app.renderer.show_system.assert_not_called()

    def test_show_system_falls_back_to_renderer(self):
        """_show_system deve usar renderer.show_system se show_system_message não existir."""
        app = _make_app()
        orchestrator = _make_orchestrator(app)

        orchestrator._show_system("teste")

        app.renderer.show_system.assert_called_once_with("teste")

    def test_show_system_falls_back_to_renderer_on_attribute_error(self):
        """_show_system deve usar renderer.show_system se show_system_message levanta AttributeError."""
        app = _make_app()
        app.show_system_message = Mock(side_effect=AttributeError("stub"))
        orchestrator = _make_orchestrator(app)

        orchestrator._show_system("teste")

        app.renderer.show_system.assert_called_once_with("teste")

if __name__ == "__main__":
    unittest.main()
