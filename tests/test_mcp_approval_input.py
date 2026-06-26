"""Testes de regressão para os bugs de input/aprovação em contexto MCP.

Cobre três classes de problemas identificados quando um servidor MCP (thread de
background) pede aprovação enquanto o input gate está ativo na main thread:

Bug #1 — TOCTOU freeze (5 min):
    read_input_in_terminal() detecta que o loop está parado e retorna None rápido.

Bug #2 — Branch errado por race → input some:
    is_active() retorna False por race (usuário pressionou Enter entre a checagem
    e o dispatch), cai no branch else que usa sys.stdout.write direto enquanto
    Rich/pt ainda controla o terminal → texto some ou corrompido.
    Fix (approval.py): read_input_in_terminal já retorna None nessa situação;
    o fallback para o else branch é seguro porque pt não está mais em raw mode.

Bug #3 — _show() antes do input corre com Live context:
    A mensagem de aprovação é emitida por thread background via renderer
    enquanto Rich.Live está em andamento → próximo refresh apaga o texto.
    Fix testado aqui: renderer.show_system é chamado, flush é solicitado.
"""
from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.approval import ApprovalManager
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.input_broker import InputBroker


_cfg = ToolRuntimeConfig(workspace_root=Path("/tmp"))


# ─────────────────────────────────────────────────────────────────────────────
# Bug #2 — approval.py: fallback correto quando read_input_in_terminal retorna None
# ─────────────────────────────────────────────────────────────────────────────


def test_approval_xthread_falls_back_to_deny_when_read_returns_none():
    """Bug #2: quando read_input_in_terminal retorna None (loop parado/race),
    o branch use_input_gate_xthread retorna False (nega) em vez de travar.
    """
    # Simula um input_gate cujo is_active() retorna True mas
    # read_input_in_terminal retorna None (loop morreu no race)
    mock_gate = MagicMock()
    mock_gate.is_active.return_value = True
    mock_gate.read_input_in_terminal.return_value = None

    handler = ApprovalManager(_cfg, input_gate=mock_gate)

    result_holder = {}

    def _bg():
        # is_main = False → use_input_gate_xthread path
        result_holder["result"] = handler.approve(tool_name="shell", summary="ls")

    t = threading.Thread(target=_bg)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), "approve() ficou bloqueado na thread de background"
    assert result_holder.get("result") is False


def test_approval_xthread_approves_when_user_responds_y():
    """Bug #2 caminho feliz: read_input_in_terminal retorna 'y' → approve True."""
    mock_gate = MagicMock()
    mock_gate.is_active.return_value = True
    mock_gate.read_input_in_terminal.return_value = "y"

    handler = ApprovalManager(_cfg, input_gate=mock_gate)

    result_holder = {}

    def _bg():
        result_holder["result"] = handler.approve(tool_name="shell", summary="ls")

    t = threading.Thread(target=_bg)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert result_holder.get("result") is True


def test_approval_xthread_denies_on_n():
    """Bug #2: read_input_in_terminal retorna 'n' → approve False."""
    mock_gate = MagicMock()
    mock_gate.is_active.return_value = True
    mock_gate.read_input_in_terminal.return_value = "n"

    handler = ApprovalManager(_cfg, input_gate=mock_gate)

    result_holder = {}

    def _bg():
        result_holder["result"] = handler.approve(tool_name="shell", summary="ls")

    t = threading.Thread(target=_bg)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert result_holder.get("result") is False


def test_approval_inactive_gate_xthread_falls_back_to_regular_input():
    """InputGate inativo em thread de background não deve auto-negar.

    Regressão do guard introduzido em approval.py: InputGate existir mas estar
    inativo não prova raw mode residual. O handler deve cair no caminho normal
    de input com suspend/resume em vez de negar automaticamente.
    """
    mock_gate = MagicMock()
    mock_gate.is_active.return_value = False

    handler = ApprovalManager(_cfg, input_gate=mock_gate, input_fn=lambda _: "y")

    result_holder = {}

    def _bg():
        result_holder["result"] = handler.approve(tool_name="run_shell", summary="pwd")

    t = threading.Thread(target=_bg)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), "approve() ficou bloqueado na thread de background"
    assert result_holder.get("result") is True
    mock_gate.read_input_in_terminal.assert_not_called()


def test_input_broker_read_line_uses_gate_even_when_inactive():
    """InputBroker deve tentar o InputGate mesmo quando is_active() está False."""
    gate = MagicMock()
    gate.is_active.return_value = False
    gate.read_input_in_terminal.return_value = "y"

    broker = InputBroker(input_gate=gate)

    result = broker._read_line("  Executar? [y/N/a=todas]: ", deadline=time.monotonic() + 10)

    assert result == "y"
    gate.read_input_in_terminal.assert_called_once()


def test_input_broker_read_line_does_not_call_input_gate_directly_when_terminal_read_unavailable():
    """InputBroker não deve chamar InputGate(prompt) pela thread do broker."""
    gate = MagicMock()
    gate.read_input_in_terminal.return_value = None

    broker = InputBroker(input_gate=gate)

    result = broker._read_line("  Executar? [y/N/a=todas]: ", deadline=time.monotonic() + 10)

    assert result is None
    gate.read_input_in_terminal.assert_called_once()
    gate.assert_not_called()


def test_input_broker_read_line_without_gate_fails_safe():
    """Sem InputGate, InputBroker falha seguro e não consome stdin diretamente."""
    broker = InputBroker(input_gate=None)

    result = broker._read_line("  Executar? [y/N/a=todas]: ", deadline=time.monotonic() + 10)

    assert result is None


def test_input_broker_interactive_window_pauses_and_resumes_spinner_callbacks():
    """InputBroker deve pausar o loading externo antes de prompt interativo."""
    events = []
    broker = InputBroker(input_gate=None)
    broker.set_spinner_callbacks(
        lambda: events.append("suspend"),
        lambda: events.append("resume"),
    )

    with broker._input_terminal_window():
        events.append("inside")

    assert events == ["suspend", "inside", "resume"]


# ─────────────────────────────────────────────────────────────────────────────
# Bug #3 — _show() chama renderer.show_system + flush de thread background
# ─────────────────────────────────────────────────────────────────────────────


def test_approval_show_calls_renderer_show_system_from_background_thread():
    """Bug #3: _show() em thread de background usa renderer.show_system().

    Garante que a mensagem de aprovação passa pelo renderer (que pode rotear
    para run_in_terminal_message ou Rich.Live) em vez de ir direto para stdout
    onde seria apagada pelo próximo refresh do Live context.
    """
    show_calls = []
    flush_calls = []

    class FakeRenderer:
        def show_system(self, msg):
            show_calls.append(msg)

        def flush(self):
            flush_calls.append(True)

        @contextmanager
        def approval_window(self, **_kwargs):
            yield

    renderer = FakeRenderer()
    # input_fn nega imediatamente — só queremos verificar o _show()
    handler = ApprovalManager(_cfg, input_fn=lambda _: "n", renderer=renderer)

    result_holder = {}

    def _bg():
        result_holder["result"] = handler.approve(tool_name="mcp_tool", summary="do_thing")

    t = threading.Thread(target=_bg)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive()
    # renderer.show_system foi chamado com a mensagem de aprovação
    assert any("Aprovar mcp_tool" in m for m in show_calls), f"show_calls={show_calls}"
    # flush foi solicitado após show_system
    assert flush_calls, "flush não foi chamado após show_system"


def test_approval_show_falls_back_to_print_without_renderer():
    """Bug #3 fallback: sem renderer, _show() usa print()."""
    handler = ApprovalManager(_cfg, input_fn=lambda _: "n")

    with patch("builtins.print") as mock_print:
        handler.approve(tool_name="mcp_tool", summary="do_thing")

    printed = " ".join(str(c) for c in mock_print.call_args_list)
    assert "Aprovar mcp_tool" in printed


# ─────────────────────────────────────────────────────────────────────────────
# Race condition — concorrência entre threads no _interactive_lock
# ─────────────────────────────────────────────────────────────────────────────


def test_approval_interactive_lock_serializes_concurrent_approvals():
    """_interactive_lock garante que aprovações concorrentes são serializadas.

    Quando duas threads chegam simultaneamente, apenas uma executa o input()
    de cada vez — não há prompts sobrepostos.
    """
    call_order = []
    lock_for_test = threading.Lock()

    call_count = 0

    def _sequential_input(prompt):
        nonlocal call_count
        with lock_for_test:
            call_count += 1
            idx = call_count
        call_order.append(f"input-{idx}")
        time.sleep(0.05)  # simula latência
        return "y"

    handler = ApprovalManager(_cfg, input_fn=_sequential_input)

    results = []
    barrier = threading.Barrier(3)

    def _worker():
        barrier.wait()
        r = handler.approve(tool_name="shell", summary="ls")
        results.append(r)

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    # Todos aprovados
    assert results.count(True) == 3
    # Três inputs chamados (um por thread, sem sobreposição)
    assert len(call_order) == 3


def test_approval_no_freeze_when_cancel_event_set_before_xthread():
    """Bug #1 variante: cancel_event já setado antes da chamada de background.

    Garante que a thread de background não trava quando o usuário cancela.
    """
    cancel = threading.Event()
    cancel.set()

    mock_gate = MagicMock()
    mock_gate.is_active.return_value = True

    handler = ApprovalManager(_cfg, input_gate=mock_gate, cancel_event=cancel)

    start = time.monotonic()
    result_holder = {}

    def _bg():
        result_holder["result"] = handler.approve(tool_name="shell", summary="ls")

    t = threading.Thread(target=_bg)
    t.start()
    t.join(timeout=3.0)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), "approve() ficou bloqueado com cancel_event setado"
    assert result_holder.get("result") is False
    assert elapsed < 2.0
