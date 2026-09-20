"""Regressão: prompts de aprovação órfãos de execuções canceladas.

Cenário reportado: agente principal delega em paralelo, o timeout cancela os
steps e o run finaliza; pedidos de aprovação enfileirados pelos steps ainda
vivos apareciam ao usuário depois do fim do run e, mesmo aprovados, falhavam
em sequência. Pedidos vinculados a um cancel_event sinalizado devem ser
negados automaticamente sem exibir prompt.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from quimera.runtime.approval import ApprovalManager
from quimera.runtime.input_broker import InputBroker, _InputRequest


class _InactiveGate:
    """Gate inativo: o consumer do broker não pode processar (requeue loop)."""

    def is_active(self) -> bool:
        return False


def _make_broker() -> InputBroker:
    return InputBroker(renderer=None, input_gate=_InactiveGate())


class TestInputBrokerCancelledRequests:
    def test_request_approval_denies_immediately_when_already_cancelled(self):
        broker = _make_broker()
        cancelled = threading.Event()
        cancelled.set()
        start = time.monotonic()
        result = broker.request_approval(
            "delegate",
            "delegate(alvo)",
            source="agente-x",
            timeout=30.0,
            cancel_event=cancelled,
        )
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 1.0, "não deve aguardar timeout nem prompt"

    def test_queued_approval_wakes_and_denies_when_cancelled_later(self):
        broker = _make_broker()
        cancel_event = threading.Event()
        results: list[bool] = []

        def _request() -> None:
            results.append(
                broker.request_approval(
                    "delegate",
                    "delegate(alvo)",
                    source="agente-x",
                    timeout=30.0,
                    cancel_event=cancel_event,
                )
            )

        t = threading.Thread(target=_request, daemon=True)
        t.start()
        time.sleep(0.3)  # pedido enfileirado, gate inativo → ninguém processa
        assert not results, "pedido deve estar bloqueado aguardando resposta"
        cancel_event.set()
        t.join(timeout=3.0)
        assert not t.is_alive(), "wait() deve acordar após cancelamento"
        assert results == [False]

    def test_process_pending_once_skips_cancelled_request_without_prompt(self):
        gate = MagicMock()
        gate.is_active.return_value = False
        broker = InputBroker(renderer=None, input_gate=gate)
        cancel_event = threading.Event()
        req = _InputRequest(
            kind="approval",
            source="agente-x",
            question="delegate?",
            options=[],
            timeout=30.0,
            default=False,
            cancel_event=cancel_event,
        )
        cancel_event.set()
        broker._queue.put(req)
        handled = broker.process_pending_once()
        assert handled is False
        assert req.is_done()
        assert req.wait() is False
        gate.read_approval_in_terminal.assert_not_called()
        gate.read_input_in_terminal.assert_not_called()

    def test_uncancelled_request_still_respects_timeout_default(self):
        broker = _make_broker()
        start = time.monotonic()
        result = broker.request_approval(
            "run_shell",
            "pwd",
            source="agente-x",
            timeout=0.5,
            cancel_event=threading.Event(),
        )
        assert result is False
        assert time.monotonic() - start >= 0.4


class TestConsoleApprovalCancelledRun:
    def test_approve_via_broker_denies_without_enqueue_when_cancelled(self):
        fake_broker = MagicMock()
        cancelled = threading.Event()
        cancelled.set()
        manager = ApprovalManager(
            None, input_broker=fake_broker, cancel_event=cancelled
        )
        result = manager.approve(tool_name="delegate", summary="delegate(alvo)")
        assert result is False
        fake_broker.request_approval.assert_not_called()

    def test_approve_via_broker_propagates_thread_bound_cancel_event(self):
        fake_broker = MagicMock()
        fake_broker.request_approval.return_value = True
        manager = ApprovalManager(None, input_broker=fake_broker)
        cancel_event = threading.Event()
        previous = manager.bind_cancel_event(cancel_event)
        try:
            result = manager.approve(tool_name="delegate", summary="delegate(alvo)")
        finally:
            manager.bind_cancel_event(previous)
        assert result is True
        kwargs = fake_broker.request_approval.call_args.kwargs
        assert kwargs["cancel_event"] is cancel_event

    def test_approve_falls_back_for_legacy_broker_without_cancel_kwarg(self):
        class LegacyBroker:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def request_approval(self, tool_name, summary, *, source, on_approve_all=None):
                self.calls.append((tool_name, summary, source))
                return True

        legacy = LegacyBroker()
        manager = ApprovalManager(None, input_broker=legacy)
        result = manager.approve(tool_name="run_shell", summary="pwd")
        assert result is True
        assert legacy.calls == [("run_shell", "pwd", "aprovação")]
