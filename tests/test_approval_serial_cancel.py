"""Testes de serialização (lock) e cancelamento durante prompt ativo com input_gate."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from quimera.runtime.approval import ConsoleApprovalHandler


class TestConsoleApprovalHandlerSerialLock:
    """Testes do lock de serialização em _approve_interactive."""

    def test_serial_lock_denies_concurrent_approval(self):
        """Quando duas threads chamam approve() concorrentemente, a segunda
        deve retornar False imediatamente (lock não-bloqueante)."""
        gate_calls = []
        gate_lock = threading.Event()

        def slow_gate(prompt):
            gate_calls.append(prompt)
            gate_lock.wait(5)  # trava até a main thread liberar
            return "n"

        handler = ConsoleApprovalHandler(input_gate=slow_gate)

        results = []

        def call_approve():
            with patch("builtins.print"):
                r = handler.approve(tool_name="tool1", summary="test")
                results.append(r)

        t = threading.Thread(target=call_approve, daemon=True)
        t.start()

        # Espera a thread entrar no gate
        time.sleep(0.3)
        assert len(gate_calls) == 1, "gate deve ter sido chamado 1x"

        # Segunda chamada deve retornar False imediatamente (lock não-bloqueante)
        with patch("builtins.print"):
            r2 = handler.approve(tool_name="tool2", summary="test2")
        assert r2 is False, "segunda chamada concorrente deve retornar False"
        assert len(gate_calls) == 1, "gate não pode ter sido chamado de novo"

        # Libera a thread presa
        gate_lock.set()
        t.join(3)
        assert results == [False], "primeira chamada deve retornar False (digitou 'n')"

    def test_serial_lock_allows_sequential_approvals(self):
        """Chamadas sequenciais (não concorrentes) funcionam normalmente."""
        handler = ConsoleApprovalHandler(
            input_fn=lambda p: "y",
        )
        with patch("builtins.print"):
            r1 = handler.approve(tool_name="tool1", summary="test1")
            r2 = handler.approve(tool_name="tool2", summary="test2")
        assert r1 is True
        assert r2 is True

    def test_serial_lock_releases_on_exception(self):
        """Lock é liberado mesmo quando ocorre exceção no prompt."""
        def failing_gate(prompt):
            raise EOFError

        handler = ConsoleApprovalHandler(input_gate=failing_gate)

        with patch("builtins.print"):
            r1 = handler.approve(tool_name="tool1", summary="test")

        assert r1 is False

        # Agora o lock deve estar livre
        handler2 = ConsoleApprovalHandler(
            input_fn=lambda p: "y",
        )
        with patch("builtins.print"):
            r2 = handler2.approve(tool_name="tool2", summary="test2")
        assert r2 is True


class TestConsoleApprovalHandlerInputGateCancelDuringPrompt:
    """Testes de cancelamento durante prompt ativo com input_gate."""

    def test_input_gate_cancel_during_readline(self):
        """cancel_event.set() disparado durante prompt ativo no input_gate
        causa retorno False."""
        gate_called = threading.Event()
        can_continue = threading.Event()
        cancel_event = threading.Event()

        def gate_with_block(prompt):
            gate_called.set()
            can_continue.wait(5)
            return "n"

        handler = ConsoleApprovalHandler(
            input_gate=gate_with_block,
            cancel_event=cancel_event,
        )

        result_holder = []

        def do_approve():
            with patch("builtins.print"):
                r = handler.approve(tool_name="shell", summary="ls")
                result_holder.append(r)

        t = threading.Thread(target=do_approve, daemon=True)
        t.start()

        gate_called.wait(3)
        cancel_event.set()
        can_continue.set()
        t.join(3)
        assert result_holder == [False]

    def test_input_gate_cancel_set_before_call(self):
        """Se cancel_event já está setado antes de chamar approve() com input_gate,
        a chamada retorna False sem chamar o gate (regressão F2 coberta)."""
        mock_gate = MagicMock(return_value="y")
        cancel_event = threading.Event()
        cancel_event.set()

        handler = ConsoleApprovalHandler(
            input_gate=mock_gate,
            cancel_event=cancel_event,
        )

        with patch("builtins.print"):
            result = handler.approve(tool_name="shell", summary="ls")

        assert result is False
        mock_gate.assert_not_called()


class TestConsoleApprovalHandlerParallelApproval:
    """Simula múltiplas threads chamando approve() concorrentemente."""

    def test_parallel_approvals_only_one_wins_lock(self):
        """Duas threads chamando approve() concorrentemente: apenas uma
        executa o prompt; as demais retornam False imediatamente."""
        gate_calls = []
        gate_started = threading.Event()
        gate_done = threading.Event()
        proceed = threading.Event()
        entered_count = 0
        entered_lock = threading.Lock()

        def blocking_gate(prompt):
            nonlocal entered_count
            gate_calls.append(prompt)
            gate_started.set()
            with entered_lock:
                entered_count += 1
            proceed.wait(5)
            return "n"

        handler = ConsoleApprovalHandler(input_gate=blocking_gate)
        results = []
        threads = []

        for i in range(2):
            def approve_thread():
                with patch("builtins.print"):
                    r = handler.approve(tool_name="tool", summary="test")
                    results.append(r)

            t = threading.Thread(target=approve_thread, daemon=True)
            threads.append(t)

        for t in threads:
            t.start()

        # Aguarda a primeira thread entrar no gate
        gate_started.wait(3)
        # Pequena pausa para dar tempo da segunda thread tentar o lock
        time.sleep(0.5)

        # Libera o gate
        proceed.set()

        for t in threads:
            t.join(3)

        # Apenas 1 thread conseguiu entrar no gate (a que ganhou o lock)
        assert len(gate_calls) == 1, "apenas 1 chamada ao gate"
        assert len(results) == 2, "ambas as threads devem ter retornado"
        assert all(r is False for r in results), "ambas retornam False"
