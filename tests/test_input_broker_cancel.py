"""Regressão: prompts de aprovação órfãos de execuções canceladas.

Cenário reportado: agente principal delega em paralelo, o timeout cancela os
steps e o run finaliza; pedidos de aprovação enfileirados pelos steps ainda
vivos apareciam ao usuário depois do fim do run e, mesmo aprovados, falhavam
em sequência. Pedidos vinculados a um cancel_event sinalizado devem ser
negados automaticamente sem exibir prompt.
"""

from __future__ import annotations

from contextlib import nullcontext
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
            "tasks_delegate",
            "tasks_delegate(alvo)",
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
                    "tasks_delegate",
                    "tasks_delegate(alvo)",
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
        result = manager.approve(tool_name="tasks_delegate", summary="tasks_delegate(alvo)")
        assert result is False
        fake_broker.request_approval.assert_not_called()

    def test_approve_via_broker_propagates_thread_bound_cancel_event(self):
        fake_broker = MagicMock()
        fake_broker.request_approval.return_value = True
        manager = ApprovalManager(None, input_broker=fake_broker)
        cancel_event = threading.Event()
        previous = manager.bind_cancel_event(cancel_event)
        try:
            result = manager.approve(tool_name="tasks_delegate", summary="tasks_delegate(alvo)")
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


def test_interactive_window_does_not_write_escape_codes(capsys):
    """O renderer é dono do terminal; em pipe nada pode vazar ANSI para stdout."""
    broker = InputBroker(renderer=None, input_gate=_InactiveGate())
    with broker._with_interactive_terminal_window(lambda: nullcontext()):
        pass
    assert capsys.readouterr().out == ""


def _sequential_setup(stdin_text: str):
    import io

    from quimera.app.simple_input_gate import SimpleInputGate
    from quimera.ui import PlainTextRenderer

    out = io.StringIO()
    gate = SimpleInputGate(renderer=PlainTextRenderer(stream=out), stdin=io.StringIO(stdin_text))
    broker = InputBroker(renderer=gate._renderer, input_gate=gate)
    return broker, gate, out


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_background_ask_user_owns_the_next_stdin_line_in_pipe_mode():
    """Pedido de produtor em background com o chat ocioso: a linha é do ask_user."""
    broker, gate, out = _sequential_setup("2\nchat\n")
    result = {}

    worker = threading.Thread(
        target=lambda: result.update(
            ask=broker.request_ask_user("Qual?", ["Python", "Rust"], source="tarefa", timeout=2)
        )
    )
    worker.start()
    # A reserva precede a fila: assim que o pedido existe, o chat já não lê.
    assert _wait_until(lambda: gate._direct_waiters > 0)
    chat_line = gate.read_chat_line(timeout=1, prompt="Alex: ")
    worker.join(timeout=5)

    assert result["ask"] == (1, "Rust")
    assert chat_line == "chat"
    assert "Selecione (1-2): 2" in out.getvalue()
    assert gate._direct_waiters == 0


def test_background_approval_owns_the_next_stdin_line_in_pipe_mode():
    broker, gate, out = _sequential_setup("y\nchat\n")
    result = {}

    worker = threading.Thread(
        target=lambda: result.update(ok=broker.request_approval("run_shell", "rm -rf /tmp/x", timeout=2))
    )
    worker.start()
    assert _wait_until(lambda: gate._direct_waiters > 0)
    chat_line = gate.read_chat_line(timeout=1, prompt="Alex: ")
    worker.join(timeout=5)

    assert result["ok"] is True
    assert chat_line == "chat"
    assert "Executar? [y/N/a=todas]: y" in out.getvalue()
    assert gate._direct_waiters == 0


def test_timed_out_request_releases_the_stdin_reservation():
    broker, gate, _out = _sequential_setup("")
    # stdin vazio: o pedido expira e não pode deixar o chat preso na reserva.
    assert broker.request_ask_user("Qual?", ["Python", "Rust"], timeout=0.3) == (0, "Python")
    assert _wait_until(lambda: gate._direct_waiters == 0)


class _LiveStdin:
    """stdin de pipe vivo: só entrega linhas quando o teste as escreve."""

    def __init__(self) -> None:
        import queue

        self._lines: "queue.Queue[str]" = queue.Queue()

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        return self._lines.get()

    def write_line(self, text: str) -> None:
        self._lines.put(text + "\n")

    def close(self) -> None:
        self._lines.put("")


def _live_setup():
    import io

    from quimera.app.simple_input_gate import SimpleInputGate
    from quimera.ui import PlainTextRenderer

    out = io.StringIO()
    stdin = _LiveStdin()
    gate = SimpleInputGate(renderer=PlainTextRenderer(stream=out), stdin=stdin)
    broker = InputBroker(renderer=gate._renderer, input_gate=gate)
    return broker, gate, stdin, out


def test_expired_request_does_not_consume_a_later_line():
    """Pipe aberto sem dados: o pedido expira e a linha que chega depois é do chat."""
    broker, gate, stdin, out = _live_setup()
    try:
        started = time.monotonic()
        assert broker.request_ask_user("Qual?", ["Python", "Rust"], timeout=0.3) == (0, "Python")
        assert time.monotonic() - started < 1.5
        assert _wait_until(lambda: gate._direct_waiters == 0)

        stdin.write_line("2")
        assert gate.read_chat_line(timeout=2, prompt="Alex: ") == "2"
        assert "Selecione (1-2): 2" not in out.getvalue()
        assert out.getvalue().endswith("Alex: 2\n")
    finally:
        stdin.close()


def test_answer_arriving_at_the_deadline_is_not_lost():
    """Se a leitura já tomou a linha quando o produtor expira, a resposta vale."""
    broker, gate, stdin, out = _live_setup()
    try:
        results = []
        worker = threading.Thread(
            target=lambda: results.append(
                broker.request_ask_user("Qual?", ["Python", "Rust"], timeout=0.4)
            )
        )
        worker.start()
        assert _wait_until(lambda: "Qual?" in out.getvalue())
        time.sleep(0.3)
        stdin.write_line("2")
        worker.join(timeout=5)
        # Ou a resposta chegou a tempo (Rust) ou expirou antes (Python): nunca
        # as duas coisas, e a linha nunca sobra para o chat se foi consumida.
        assert results in ([(1, "Rust")], [(0, "Python")])
        if results == [(0, "Python")]:
            assert gate.read_chat_line(timeout=2, prompt="Alex: ") == "2"
        else:
            assert gate.read_chat_line(timeout=0.2, prompt="Alex: ") is None
        assert gate._direct_waiters == 0
    finally:
        stdin.close()


def test_concurrent_resolution_releases_the_reservation_exactly_once():
    """Produtor (timeout) e consumidor (resposta) concluindo juntos: uma só liberação."""
    import io

    from quimera.app.simple_input_gate import SimpleInputGate

    gate = SimpleInputGate(stdin=io.StringIO(""))
    for _ in range(100):
        req = _InputRequest(
            kind="ask_user", source="t", question="q", options=["a", "b"], timeout=1, default=(0, "a")
        )
        req.reservation = gate.reserve_direct_input()
        barrier = threading.Barrier(2)
        outcomes = []

        def resolve(value):
            barrier.wait()
            outcomes.append(req.set_result(value))

        threads = [threading.Thread(target=resolve, args=(value,)) for value in ((0, "a"), (1, "b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes) == [False, True]
        assert gate._direct_waiters == 0
        assert req.wait() in ((0, "a"), (1, "b"))


def test_sequential_gate_keeps_requests_in_fifo_order_and_disables_manual_pump():
    """Dois pedidos: cada um recebe a sua linha, na ordem; o pump manual não interfere."""
    broker, gate, out = _sequential_setup("2\n1\n")
    results = {}

    def ask(name, options):
        results[name] = broker.request_ask_user(name, options, timeout=3)

    assert broker.process_pending_once() is False  # gate sequencial: sem pump manual
    first = threading.Thread(target=ask, args=("Pergunta A", ["A1", "A2"]))
    first.start()
    assert _wait_until(lambda: "Pergunta A" in out.getvalue())
    second = threading.Thread(target=ask, args=("Pergunta B", ["B1", "B2"]))
    second.start()
    assert broker.process_pending_once() is False
    first.join(timeout=5)
    second.join(timeout=5)

    assert results == {"Pergunta A": (1, "A2"), "Pergunta B": (0, "B1")}
    assert gate._direct_waiters == 0


def test_claimed_line_wins_even_if_the_echo_blocks_past_the_grace():
    """Linha reclamada antes do eco: o pedido não expira, por mais que a saída trave."""
    import io

    from quimera.app.simple_input_gate import SimpleInputGate
    from quimera.ui import PlainTextRenderer

    class _BlockingStream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.gate_open = threading.Event()

        def write(self, text):
            if "Selecione" in text and not self.gate_open.is_set():
                self.gate_open.wait(timeout=10)
            return super().write(text)

    stream = _BlockingStream()
    gate = SimpleInputGate(renderer=PlainTextRenderer(stream=stream), stdin=io.StringIO("2\nchat\n"))
    broker = InputBroker(renderer=gate._renderer, input_gate=gate)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(broker.request_ask_user("Qual?", ["Python", "Rust"], timeout=0.15))
    )
    worker.start()
    # O eco "Selecione (1-2): 2" fica travado bem além do timeout e da graça.
    time.sleep(_InputRequest._EXPIRY_GRACE_SECONDS + 0.5)
    assert worker.is_alive(), "com linha reclamada o produtor espera a resposta real"
    stream.gate_open.set()
    worker.join(timeout=5)

    assert results == [(1, "Rust")]
    assert gate.read_chat_line(timeout=1, prompt="Alex: ") == "chat"
    assert gate._direct_waiters == 0
