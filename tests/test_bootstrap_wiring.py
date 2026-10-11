"""Testes do wiring de delegação (AppAssembler._wire).

Regressão para a instabilidade de delegação entre agentes CLI: toda
delegação originada de tool call precisa usar AgentClient isolado
(o run() do client principal não é reentrante), e o cancelamento do
usuário deve se propagar aos clients de background.
"""
from unittest.mock import MagicMock, call

import pytest

from quimera.app.bootstrap import wiring


@pytest.fixture
def wired(monkeypatch):
    """Executa _wire com bundles mockados e retorna (app, plat, ui, sess, rt, tasks, chat)."""
    monkeypatch.setattr(wiring, "AppLifecycle", MagicMock())
    assembler = wiring.AppAssembler.__new__(wiring.AppAssembler)
    bundles = tuple(MagicMock() for _ in range(7))
    assembler._wire(*bundles)
    return bundles


def test_wire_routes_tool_delegate_through_isolated_dispatch(wired):
    """A tool delegate (socket interno) não pode usar o delegate do fluxo principal.

    Antes, set_delegate_fn recebia dispatch_services.delegate: uma delegação
    síncrona via MCP executava o agente alvo sobre o mesmo AgentClient cujo
    run() do agente origem ainda estava ativo — limpando cancel_event,
    sobrescrevendo _current_proc e parando o EscMonitor do fluxo do chat.
    """
    _app, _plat, _ui, _sess, _rt, tasks, _chat = wired

    tasks.tool_executor.set_delegate_fn.assert_called_once()
    delegate_fn = tasks.tool_executor.set_delegate_fn.call_args[0][0]
    assert delegate_fn is not tasks.dispatch_services.delegate

    background = MagicMock()
    background.delegate.return_value = "resposta isolada"
    tasks.task_services._create_background_dispatch_services.return_value = background

    result = delegate_fn("codex", delegation={"task": "x"})

    assert result == "resposta isolada"
    background.delegate.assert_called_once()
    tasks.dispatch_services.delegate.assert_not_called()


def test_wire_does_not_fallback_to_primary_dispatch_when_background_unavailable(wired):
    """Falha de criação do background dispatch não pode reutilizar o client principal."""
    _app, _plat, _ui, _sess, _rt, tasks, _chat = wired

    tasks.tool_executor.set_delegate_fn.assert_called_once()
    delegate_fn = tasks.tool_executor.set_delegate_fn.call_args[0][0]
    tasks.task_services._create_background_dispatch_services.return_value = None

    with pytest.raises(RuntimeError, match="background"):
        delegate_fn("codex", delegation={"task": "x"})

    tasks.dispatch_services.delegate.assert_not_called()


def test_wire_uses_same_isolated_fn_for_sync_and_background_delegate(wired):
    """Caminho síncrono (socket) e assíncrono (http) compartilham a mesma closure isolada."""
    _app, _plat, _ui, _sess, _rt, tasks, _chat = wired

    delegate_fn = tasks.tool_executor.set_delegate_fn.call_args[0][0]
    background_fn = tasks.tool_executor.set_background_delegate_fn.call_args[0][0]

    assert background_fn is delegate_fn


def test_wire_binds_tasks_tool_to_canonical_task_service(wired):
    """A tool tasks reutiliza o mesmo serviço de domínio do comando /task."""
    _app, _plat, _ui, _sess, _rt, tasks, _chat = wired

    tasks.tool_executor.set_task_create_fn.assert_called_once_with(
        tasks.task_services.create_agent_task,
    )


def test_wire_registers_cancel_propagation_to_background_clients(wired):
    """ESC no fluxo principal deve cancelar todo trabalho em background."""
    _app, _plat, _ui, _sess, rt, tasks, _chat = wired

    assert rt.agent_client.add_cancel_listener.call_args_list == [
        call(tasks.task_services.cancel_background_work),
        call(tasks.debate_service.cancel_active),
    ]


class _RoutingConfig:
    """Config mínima com estado de roteamento persistido."""

    def __init__(self, frozen=None, orchestrator=None):
        self.frozen_agent = frozen
        self.orchestrator_agent = orchestrator


def test_restore_agent_routing_reaplica_orquestrador_persistido():
    """Orquestrador salvo volta ativo quando o agente segue no pool com companhia."""
    from quimera.app.agent_pool import AgentPool

    pool = AgentPool(["claude", "codex"])
    wiring.AppAssembler._restore_agent_routing(
        pool, _RoutingConfig(frozen="claude", orchestrator="claude"), ["claude", "codex"]
    )
    assert pool.orchestrator_agent == "claude"
    assert pool.frozen_agent == "claude"


def test_restore_agent_routing_reaplica_congelado_sem_orquestrador():
    """Sem orquestrador salvo, o freeze persiste sozinho."""
    from quimera.app.agent_pool import AgentPool

    pool = AgentPool(["claude", "codex"])
    wiring.AppAssembler._restore_agent_routing(
        pool, _RoutingConfig(frozen="codex"), ["claude", "codex"]
    )
    assert pool.frozen_agent == "codex"
    assert pool.orchestrator_agent is None


def test_restore_agent_routing_ignora_agente_fora_da_selecao():
    """Roteamento salvo para agente ausente da sessão não derruba o boot."""
    from quimera.app.agent_pool import AgentPool

    pool = AgentPool(["codex"])
    wiring.AppAssembler._restore_agent_routing(
        pool, _RoutingConfig(frozen="claude", orchestrator="claude"), ["codex"]
    )
    assert pool.frozen_agent is None
    assert pool.orchestrator_agent is None


def test_restore_agent_routing_orquestrador_exige_outro_agente():
    """Orquestrador sozinho no pool não é restaurado (o/ exige outro agente)."""
    from quimera.app.agent_pool import AgentPool

    pool = AgentPool(["claude"])
    wiring.AppAssembler._restore_agent_routing(
        pool, _RoutingConfig(frozen="claude", orchestrator="claude"), ["claude"]
    )
    assert pool.orchestrator_agent is None
    assert pool.frozen_agent is None


class TestBackgroundDelegateFailureReceipt:
    """O delegate_fn de background converte 'no response' em falha estruturada."""

    @staticmethod
    def _make_services(bg):
        task_services = MagicMock()
        task_services._create_background_dispatch_services.return_value = bg
        dispatch_services = MagicMock(name="primary-dispatch")
        return task_services, dispatch_services

    def test_none_vira_delegation_dispatch_error_com_recibo(self):
        from quimera.runtime.errors import DelegationDispatchError

        receipt = {
            "error_kind": "process_died",
            "exit_code": -9,
            "signal": "SIGKILL",
            "duration_s": 12.0,
        }
        bg = MagicMock()
        bg.delegate.return_value = None
        bg.consume_last_run_failure.return_value = receipt
        task_services, dispatch_services = self._make_services(bg)

        fn = wiring._make_background_delegate_fn(task_services, dispatch_services)
        with pytest.raises(DelegationDispatchError) as excinfo:
            fn("codex")

        assert excinfo.value.receipt == receipt
        message = str(excinfo.value)
        assert "process_died" in message
        assert "exit=-9/SIGKILL" in message
        bg.close.assert_called_once()

    def test_none_sem_recibo_mantem_mensagem_legada(self):
        from quimera.runtime.errors import DelegationDispatchError

        bg = MagicMock()
        bg.delegate.return_value = None
        bg.consume_last_run_failure.return_value = None
        task_services, dispatch_services = self._make_services(bg)

        fn = wiring._make_background_delegate_fn(task_services, dispatch_services)
        with pytest.raises(DelegationDispatchError) as excinfo:
            fn("codex")

        assert excinfo.value.receipt == {}
        assert "returned no response" in str(excinfo.value)

    def test_resposta_valida_nao_levanta(self):
        bg = MagicMock()
        bg.delegate.return_value = "resposta"
        task_services, dispatch_services = self._make_services(bg)

        fn = wiring._make_background_delegate_fn(task_services, dispatch_services)

        assert fn("codex") == "resposta"
        bg.consume_last_run_failure.assert_not_called()
        bg.close.assert_called_once()
