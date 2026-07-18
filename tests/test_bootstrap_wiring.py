"""Testes do wiring de delegação (AppAssembler._wire).

Regressão para a instabilidade de delegação entre agentes CLI: toda
delegação originada de tool call precisa usar AgentClient isolado
(o run() do client principal não é reentrante), e o cancelamento do
usuário deve se propagar aos clients de background.
"""
from unittest.mock import MagicMock

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


def test_wire_uses_same_isolated_fn_for_sync_and_background_delegate(wired):
    """Caminho síncrono (socket) e assíncrono (http) compartilham a mesma closure isolada."""
    _app, _plat, _ui, _sess, _rt, tasks, _chat = wired

    delegate_fn = tasks.tool_executor.set_delegate_fn.call_args[0][0]
    background_fn = tasks.tool_executor.set_background_delegate_fn.call_args[0][0]

    assert background_fn is delegate_fn


def test_wire_registers_cancel_propagation_to_background_clients(wired):
    """ESC no fluxo principal deve cancelar delegações em background."""
    _app, _plat, _ui, _sess, rt, tasks, _chat = wired

    rt.agent_client.add_cancel_listener.assert_called_once_with(
        tasks.task_services.cancel_background_work
    )
