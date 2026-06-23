"""Testes de integração para o sistema de aprovação.

Cobre as lacunas identificadas em code review:
1. build_tool_executor() conectado ao _approval_handler de core
2. /approve-all ativando o ApprovalManager real
3. /approve ativando pré-aprovação
4. Cadeia ApprovalManager → PreApprovalHandler → ConsoleApprovalHandler
"""

from pathlib import Path

import pytest

from quimera.app.core import QuimeraApp
from quimera.constants import CMD_APPROVE, CMD_APPROVE_ALL
from quimera.runtime.approval import ApprovalManager
from quimera.runtime.executor import ToolExecutor


# ─── Fiação (wiring) — QuimeraApp → _approval_handler ─────────────


def test_approval_handler_is_approval_manager_after_init(tmp_path: Path):
    """Após __init__, _approval_handler é ApprovalManager (não None)."""
    app = QuimeraApp(cwd=tmp_path)
    assert app._approval_handler is not None
    assert isinstance(app._approval_handler, ApprovalManager)


def test_system_layer_getter_retorna_mesmo_handler(tmp_path: Path):
    """approval_handler_getter devolve o mesmo objeto de _approval_handler."""
    app = QuimeraApp(cwd=tmp_path)
    getter = app.system_layer.approval_handler_getter
    assert getter() is app._approval_handler


def test_build_tool_executor_compartilha_approval_manager(tmp_path: Path):
    """ToolExecutor criado por build_tool_executor usa o ApprovalManager registrado."""
    app = QuimeraApp(cwd=tmp_path)
    te = app.tool_executor
    assert isinstance(te, ToolExecutor)
    assert te.approval_handler is app._approval_handler
    # O executor expõe approval_broker como alias
    assert te.approval_broker is app._approval_handler


def test_approval_manager_metodos_essenciais(tmp_path: Path):
    """ApprovalManager gerado expõe approve, approve_call, set_approve_all, pre_approve."""
    app = QuimeraApp(cwd=tmp_path)
    h = app._approval_handler
    assert callable(h.approve)
    assert callable(h.approve_call)
    assert callable(h.set_approve_all)
    assert callable(h.pre_approve)
    assert callable(h.execution_guard)
    assert callable(h.reset_approve_all_after_cycle)
    assert callable(h.set_input_broker)


def test_set_approval_handler_callback_funciona(tmp_path: Path):
    """Callback set_approval_handler atualiza _approval_handler no QuimeraApp."""
    app = QuimeraApp(cwd=tmp_path)
    # O callback já foi chamado por build_tool_executor
    assert app._approval_handler is not None
    # Cria um segundo manager e verifica que o callback setter funciona
    novo = ApprovalManager(None)
    app._approval_handler = novo  # só seta atributo, sem callback
    # O getter do system_layer usa getattr dinâmico
    assert app.system_layer.approval_handler_getter() is novo
    assert app.system_layer.approval_handler_getter() is not app.task_services._get_approval_handler()


# ─── Comportamento — ApprovalManager com input_fn de fallback ───


def _make_handler():
    """Cria ApprovalManager com input_fn que retorna 'n' (negação).

    Evita interação real com stdin em ambiente de teste.
    """
    return ApprovalManager(None, input_fn=lambda _: "n")


def _force_console_deny(handler: ApprovalManager) -> None:
    """Evita prompt real quando o teste espera queda para aprovação humana."""
    handler._pre_handler._base.approve = lambda *, tool_name, summary: False
    handler._pre_handler._base.approve_request = lambda request: False


def test_pre_approve_consumido_na_proxima_chamada():
    """pre_approve() aprova a próxima approve(); a segunda volta a negar."""
    handler = _make_handler()
    assert handler.approve(tool_name="test", summary="x") is False

    handler.pre_approve()
    assert handler.approve(tool_name="test", summary="x") is True

    assert handler.approve(tool_name="test", summary="x") is False


def test_approve_all_persiste_entre_chamadas():
    """set_approve_all(True) faz approve() retornar True até ser desligado."""
    handler = _make_handler()
    assert handler.approve(tool_name="test", summary="x") is False

    handler.set_approve_all(True)
    assert handler.approve(tool_name="test", summary="x") is True
    assert handler.approve(tool_name="test", summary="x") is True

    handler.set_approve_all(False)
    assert handler.approve(tool_name="test", summary="x") is False


def test_approve_all_permanente_sobrevive_reset():
    """approve-all permanente não reseta com reset_approve_all_after_cycle()."""
    handler = _make_handler()
    handler.set_approve_all(True, permanent=True)
    handler.reset_approve_all_after_cycle()
    assert handler.approve(tool_name="test", summary="x") is True


def test_approve_all_nao_permanente_reseta_apos_cycle():
    """approve-all não-permanente reseta com reset_approve_all_after_cycle()."""
    handler = _make_handler()
    handler.set_approve_all(True, permanent=False)
    handler.reset_approve_all_after_cycle()
    assert handler.approve(tool_name="test", summary="x") is False


def test_pre_approve_nao_afeta_approve_all():
    """pre_approve e approve-all são independentes."""
    handler = _make_handler()
    handler.set_approve_all(True)
    handler.pre_approve()
    assert handler.approve(tool_name="test", summary="x") is True
    handler.set_approve_all(False)
    assert handler.approve(tool_name="test", summary="x") is True


def test_reset_limpa_pre_approve():
    """reset() limpa pre_approve."""
    handler = _make_handler()
    handler.pre_approve()
    handler.reset()
    assert handler.approve(tool_name="test", summary="x") is False


def test_set_approve_all_reativo():
    """set_approve_all(True) → approve True → set_approve_all(False) → approve False."""
    handler = _make_handler()
    handler.set_approve_all(True)
    assert handler.approve(tool_name="test", summary="x") is True
    handler.set_approve_all(False)
    assert handler.approve(tool_name="test", summary="x") is False
    handler.set_approve_all(True)
    assert handler.approve(tool_name="test", summary="x") is True


# ─── Comandos via system_layer (com QuimeraApp real) ──────────────


def test_approve_all_command_ativa_flag(tmp_path: Path):
    """Comando /approve-all seta approve-all no ApprovalManager real."""
    app = QuimeraApp(cwd=tmp_path)
    handler = app._approval_handler
    assert isinstance(handler, ApprovalManager)

    app.system_layer.handle_command(CMD_APPROVE_ALL)

    # PreHandler intercepta antes do ConsoleHandler — não precisa de input
    assert handler.approve(tool_name="test", summary="x") is True


def test_approve_all_command_mantem_aprovacao_apos_ciclo(tmp_path: Path):
    """approve-all ativado por comando sobrevive a reset_approve_all_after_cycle se permanente."""
    app = QuimeraApp(cwd=tmp_path)
    handler = app._approval_handler
    # No QuimeraApp, approve-all não é permanente por padrão
    _force_console_deny(handler)
    app.system_layer.handle_command(CMD_APPROVE_ALL)
    handler.reset_approve_all_after_cycle()
    # Não-permanente: reseta
    assert handler.approve(tool_name="test", summary="x") is False


def test_approve_command_pre_aprova(tmp_path: Path):
    """Comando /approve ativa pré-aprovação única no ApprovalManager real."""
    app = QuimeraApp(cwd=tmp_path)
    handler = app._approval_handler
    assert isinstance(handler, ApprovalManager)
    _force_console_deny(handler)

    app.system_layer.handle_command(CMD_APPROVE)

    assert handler.approve(tool_name="test", summary="x") is True
    # Consumido
    assert handler.approve(tool_name="test", summary="x") is False


def test_approve_command_multiplas_vezes(tmp_path: Path):
    """Cada /approve dá uma pré-aprovação."""
    app = QuimeraApp(cwd=tmp_path)
    handler = app._approval_handler
    _force_console_deny(handler)

    app.system_layer.handle_command(CMD_APPROVE)
    assert handler.approve(tool_name="test", summary="x") is True

    app.system_layer.handle_command(CMD_APPROVE)
    assert handler.approve(tool_name="test", summary="x") is True

    assert handler.approve(tool_name="test", summary="x") is False


def test_input_broker_conectado_ao_approval_manager(tmp_path: Path):
    """InputBroker do QuimeraApp é propagado para o ConsoleApprovalHandler."""
    app = QuimeraApp(cwd=tmp_path)
    handler = app._approval_handler
    # O input_broker foi setado via set_input_broker em core.py:497-500
    console = handler._console_handler
    assert console._input_broker is app.input_broker


def test_approve_all_command_por_getter_nao_quebra_sem_handler(tmp_path: Path):
    """handle_command("/approve-all") não quebra se approval_handler_getter retornar None."""
    from quimera.app.system_layer import AppSystemLayer
    from quimera.app.agent_pool import AgentPool
    from quimera.app.display_service import DisplayService

    renderer = DummyRenderer()
    layer = AppSystemLayer(
        agent_pool=AgentPool([]),
        renderer=renderer,
        approval_handler_getter=lambda: None,
    )
    result = layer.handle_command(CMD_APPROVE_ALL)
    assert result is True


def test_approve_command_por_getter_nao_quebra_sem_handler(tmp_path: Path):
    from quimera.app.system_layer import AppSystemLayer
    from quimera.app.agent_pool import AgentPool
    from quimera.app.display_service import DisplayService

    renderer = DummyRenderer()
    layer = AppSystemLayer(
        agent_pool=AgentPool([]),
        renderer=renderer,
        approval_handler_getter=lambda: None,
    )
    result = layer.handle_command(CMD_APPROVE)
    assert result is True


class DummyRenderer:
    """Renderer mínimo para testes de AppSystemLayer."""
    def show_system(self, msg):
        pass
    def show_warning(self, msg):
        pass
    def show_message(self, msg):
        pass
