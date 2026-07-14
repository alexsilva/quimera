"""Testes unitários para ApprovalManager (quimera.runtime.approval).

Cobre todos os fluxos: aprovação interativa, pré-aprovação, approve-all,
escopo por thread, cancelamento e governança.
"""
import io
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock, patch, call

import pytest

from quimera.runtime.approval import (
    ApprovalHandler,
    ApprovalManager,
    _ApprovalCancelled,
    _emit_approval_message,
    format_approval_question,
)
from quimera.ui.base import RendererBase


# ── ApprovalHandler (abstract) ──────────────────────────────


def test_format_approval_question_compacts_risk_header():
    result = format_approval_question(
        "run_shell",
        "risco: shell\ncomando: pytest tests/test_runtime_tool_preview.py -q",
    )

    assert result == (
        "\nAprovar run_shell :: risco: shell\n"
        "comando: pytest tests/test_runtime_tool_preview.py -q"
    )


def test_format_approval_question_preserves_summary_without_risk():
    result = format_approval_question("shell", "ls")

    assert result == "\nAprovar shell\nls"

def test_approval_handler_abstract():
    """Classe base levanta NotImplementedError."""
    class ConcreteHandler(ApprovalHandler):
        def approve(self, *, tool_name: str, summary: str) -> bool:
            return super().approve(tool_name=tool_name, summary=summary)

    handler = ConcreteHandler()
    with pytest.raises(NotImplementedError):
        handler.approve(tool_name="test", summary="test")


# ── ApprovalManager (interactive) ──────────────────────────


@patch('builtins.input')
@patch('builtins.print')
def test_console_approval_handler_yes(mock_print, mock_input):
    """Resposta 'y' aprova."""
    handler = ApprovalManager(None)
    mock_input.return_value = "y"
    assert handler.approve(tool_name="shell", summary="ls") is True
    mock_print.assert_called()


@patch('builtins.input')
def test_console_approval_handler_no(mock_input):
    """Resposta 'n' nega."""
    handler = ApprovalManager(None)
    mock_input.return_value = "n"
    assert handler.approve(tool_name="shell", summary="rm -rf /") is False


@patch('builtins.input')
def test_console_approval_handler_empty(mock_input):
    """Resposta vazia nega."""
    handler = ApprovalManager(None)
    mock_input.return_value = ""
    assert handler.approve(tool_name="shell", summary="echo") is False


def test_console_approval_handler_with_custom_input_fn():
    """Quando input_fn é injetada, deve usá-la em vez de input() builtin."""
    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "yes"

    handler = ApprovalManager(None, input_fn=fake_input)
    result = handler.approve(tool_name="shell", summary="ls")

    assert result is True
    assert len(calls) == 1
    assert "Executar?" in calls[0]
    assert "y/N" in calls[0]


def test_console_approval_handler_accepts_sim():
    """Aceita 'sim' (português) como resposta afirmativa."""
    handler = ApprovalManager(None, input_fn=lambda _: "sim")
    assert handler.approve(tool_name="shell", summary="ls") is True


def test_console_approval_handler_accepts_s():
    """Aceita 's' como resposta afirmativa."""
    handler = ApprovalManager(None, input_fn=lambda _: "s")
    assert handler.approve(tool_name="shell", summary="ls") is True


def test_console_approval_handler_accepts_yes():
    """Aceita 'yes' como resposta afirmativa."""
    handler = ApprovalManager(None, input_fn=lambda _: "yes")
    assert handler.approve(tool_name="shell", summary="ls") is True


def test_console_approval_handler_rejects_uppercase_n():
    """'N' (maiúsculo) nega."""
    handler = ApprovalManager(None, input_fn=lambda _: "N")
    assert handler.approve(tool_name="shell", summary="ls") is False


def test_console_approval_handler_eof_error_returns_false():
    """EOFError (stdin fechado) retorna False."""
    handler = ApprovalManager(None, input_fn=lambda _: (_ for _ in ()).throw(EOFError()))
    with patch('builtins.print'):
        assert handler.approve(tool_name="shell", summary="ls") is False


# ── ApprovalManager + suspend/resume ────────────────────────


def test_console_approval_handler_suspend_resume_called():
    """suspend_fn e resume_fn são chamadas durante a aprovação."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: "y",
        suspend_fn=lambda: calls.append("suspend"),
        resume_fn=lambda: calls.append("resume"),
    )
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    assert calls == ["suspend", "resume"]


def test_console_approval_handler_suspend_resume_on_eof():
    """Mesmo com EOFError, resume_fn é chamada no finally."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
        suspend_fn=lambda: calls.append("suspend"),
        resume_fn=lambda: calls.append("resume"),
    )
    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert calls == ["suspend", "resume"]


def test_console_approval_handler_suspend_resume_on_deny():
    """Mesmo com negação, resume_fn é chamada no finally."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: "n",
        suspend_fn=lambda: calls.append("suspend"),
        resume_fn=lambda: calls.append("resume"),
    )
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert calls == ["suspend", "resume"]


def test_console_approval_handler_suspend_before_input():
    """suspend_fn é chamada antes de input_fn."""
    call_order = []

    def suspend_fn():
        call_order.append("suspend")

    def input_fn(prompt):
        call_order.append("input")
        return "y"

    def resume_fn():
        call_order.append("resume")

    handler = ApprovalManager(None,
        input_fn=input_fn,
        suspend_fn=suspend_fn,
        resume_fn=resume_fn,
    )
    handler.approve(tool_name="shell", summary="ls")
    assert call_order == ["suspend", "input", "resume"]


# ── ApprovalManager + spinner callbacks ─────────────────────


def test_console_approval_handler_spinner_callbacks_called():
    """Spinner callbacks são chamados na ordem: suspend_spinner → input → resume_spinner → resume."""
    calls = []
    handler = ApprovalManager(None, input_fn=lambda _: "y")
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls
    # Ordem: suspend_spinner antes de resume_spinner
    assert calls.index("suspend_spinner") < calls.index("resume_spinner")


def test_console_approval_handler_spinner_resume_on_eof():
    """Spinner resume é chamado mesmo com EOFError."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
    )
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert "resume_spinner" in calls


def test_console_approval_handler_spinner_suspend_before_resume():
    """Spinner resume é chamado mesmo quando não há suspend configurado."""
    calls = []
    handler = ApprovalManager(None, input_fn=lambda _: "n")
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    handler.approve(tool_name="shell", summary="ls")
    assert calls == ["suspend_spinner", "resume_spinner"]


def test_console_approval_handler_spinner_callbacks_not_set():
    """Sem spinner callbacks, nada quebra."""
    handler = ApprovalManager(None, input_fn=lambda _: "y")
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    # Não deve lançar AttributeError


def test_console_approval_handler_spinner_and_suspend_order():
    """Ordem completa: suspend_fn → suspend_spinner → input → resume_spinner → resume_fn."""
    order = []
    handler = ApprovalManager(None,
        input_fn=lambda _: order.append("input") or "y",
        suspend_fn=lambda: order.append("suspend_fn"),
        resume_fn=lambda: order.append("resume_fn"),
    )
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: order.append("suspend_spinner"),
        resume_spinner_fn=lambda: order.append("resume_spinner"),
    )
    handler.approve(tool_name="shell", summary="ls")
    assert order == [
        "suspend_fn",
        "suspend_spinner",
        "input",
        "resume_spinner",
        "resume_fn",
    ]


def test_console_approval_handler_input_gate_spinner_callbacks_called():
    """Com input_gate, spinner callbacks também são acionados (regressão P4)."""
    order = []

    def gate(_prompt):
        order.append("input_gate")
        return "y"

    handler = ApprovalManager(None, input_gate=gate)
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: order.append("suspend_spinner"),
        resume_spinner_fn=lambda: order.append("resume_spinner"),
    )
    with patch("builtins.print"):
        result = handler.approve(tool_name="shell", summary="ls")

    assert result is True
    assert order == ["suspend_spinner", "input_gate", "resume_spinner"]


# ── ApprovalManager + renderer ──────────────────────────────


def test_console_approval_handler_with_renderer():
    """Quando um renderer é injetado, usa show_approval em vez de print."""
    class FakeRenderer(RendererBase):
        def __init__(self):
            self.calls = []

        def show_approval(self, msg):
            self.calls.append(msg)

        @contextmanager
        def approval_window(self, **_kwargs):
            self.calls.append("approval_window:enter")
            try:
                yield
            finally:
                self.calls.append("approval_window:exit")

    renderer = FakeRenderer()
    handler = ApprovalManager(None, input_fn=lambda _: "y", renderer=renderer)
    with patch('builtins.print') as mock_print:
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    assert len(renderer.calls) >= 1
    assert "Aprovar shell" in renderer.calls[0]


def test_console_approval_handler_renderer_flushes_before_input():
    """Com renderer, flush é chamado antes de solicitar input."""
    class FakeRenderer(RendererBase):
        def __init__(self):
            self.calls = []

        def show_approval(self, msg):
            self.calls.append(("show", msg))

        def flush(self):
            self.calls.append(("flush", None))

        @contextmanager
        def approval_window(self, **_kwargs):
            self.calls.append(("approval_window:enter", None))
            try:
                yield
            finally:
                self.calls.append(("approval_window:exit", None))

    renderer = FakeRenderer()
    order = []
    handler = ApprovalManager(None,
        input_fn=lambda _: order.append("input") or "y",
        renderer=renderer,
    )
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    assert renderer.calls[:2] == [("show", "\nAprovar shell\nls"), ("flush", None)]
    assert order == ["input"]


def test_console_approval_handler_renderer_shows_eof_message():
    """Com renderer, mensagem de EOF também usa show_approval."""
    class FakeRenderer(RendererBase):
        def __init__(self):
            self.calls = []

        def show_approval(self, msg):
            self.calls.append(msg)

        @contextmanager
        def approval_window(self, **_kwargs):
            self.calls.append("approval_window:enter")
            try:
                yield
            finally:
                self.calls.append("approval_window:exit")

    renderer = FakeRenderer()
    handler = ApprovalManager(None,
        input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
        renderer=renderer,
    )
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert any("stdin não disponível" in m for m in renderer.calls)


def test_console_approval_handler_no_renderer_uses_print():
    """Sem renderer, usa print() builtin."""
    handler = ApprovalManager(None, input_fn=lambda _: "y")
    with patch('builtins.print') as mock_print:
        handler.approve(tool_name="shell", summary="ls")
    mock_print.assert_called()
    found = any(
        "Aprovar shell" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


# ── ApprovalManager + None input_fn usa builtins.input ──────


@patch('builtins.input')
@patch('builtins.print')
def test_console_approval_handler_none_input_fn_uses_builtin(mock_print, mock_input):
    """Quando input_fn=None no construtor, usa input() builtin dinamicamente."""
    handler = ApprovalManager(None, input_fn=None)
    mock_input.return_value = "y"
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    mock_input.assert_called_once()


@patch('builtins.print')
@patch('builtins.input')
def test_console_approval_handler_cancel_event_pre_set_skips_builtin_input(mock_input, _mock_print):
    """Com cancel_event já setado, approve interrompe sem bloquear no input()."""
    cancel_event = threading.Event()
    cancel_event.set()
    handler = ApprovalManager(None, input_fn=None, cancel_event=cancel_event)

    result = handler.approve(tool_name="shell", summary="ls")

    assert result is False
    mock_input.assert_not_called()


@patch('builtins.print')
def test_console_approval_handler_cancel_event_during_polling_returns_false(_mock_print):
    """Quando cancel_event é acionado durante polling, approve retorna False rapidamente."""
    cancel_event = threading.Event()
    handler = ApprovalManager(None, input_fn=None, cancel_event=cancel_event, cancel_poll_interval=0.01)

    class FakeStdin:
        @staticmethod
        def isatty():
            return True

        @staticmethod
        def fileno():
            return 0

        @staticmethod
        def readline():
            return "y\n"

    calls = {"count": 0}

    def _fake_select(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            cancel_event.set()
        return ([], [], [])

    fake_stdout = io.StringIO()
    with patch("sys.stdin", FakeStdin()), patch("sys.stdout", fake_stdout), patch("select.select", side_effect=_fake_select):
        result = handler.approve(tool_name="shell", summary="ls")

    assert result is False
    assert calls["count"] >= 1


# ── ApprovalManager approve_all ─────────────────────────────


def test_auto_approval_handler_true():
    """approve_all=True sempre aprova."""
    handler = ApprovalManager(None)
    handler.set_approve_all(True)
    assert handler.approve(tool_name="shell", summary="ls") is True
    assert handler.approve(tool_name="write_file", summary="danger") is True


def test_auto_approval_handler_false():
    """Sem approve_all, ApprovalManager delega à aprovação interativa."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")
    assert handler.approve(tool_name="shell", summary="ls") is False
    assert handler.approve(tool_name="write_file", summary="danger") is False


@patch('builtins.print')
def test_auto_approval_handler_prints_status(mock_print):
    """ApprovalManager imprime status de approve-all."""
    handler = ApprovalManager(None)
    handler.set_approve_all(True)
    handler.approve(tool_name="shell", summary="ls")
    found = any(
        "[approve-all]" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


# ── ApprovalManager pre-approve ─────────────────────────────


def test_pre_approval_handler_delegates_when_not_pre_approved():
    """Sem pré-aprovação, delega à aprovação interativa."""
    handler = ApprovalManager(None, input_fn=lambda _: "y")
    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is True


def test_pre_approval_handler_consumes_pre_approval():
    """Pré-aprovação é consumida uma única vez e depois resetada."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")
    handler.pre_approve()
    with patch('builtins.print'):
        assert handler.approve(tool_name="shell", summary="ls") is True
        assert handler.approve(tool_name="shell", summary="ls") is False


def test_pre_approval_handler_reset():
    """Reset descarta a pré-aprovação sem consumir."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")
    handler.pre_approve()
    handler.reset()
    with patch('builtins.print'):
        assert handler.approve(tool_name="shell", summary="ls") is False


def test_pre_approval_handler_multiple_pre_approvals():
    """Cada pre_approve() é consumida individualmente."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")
    with patch('builtins.print'):
        handler.pre_approve()
        assert handler.approve(tool_name="a", summary="1") is True
        assert handler.approve(tool_name="b", summary="2") is False
        handler.pre_approve()
        handler.pre_approve()
        assert handler.approve(tool_name="c", summary="3") is True
        assert handler.approve(tool_name="d", summary="4") is False


def test_pre_approval_handler_is_thread_safe():
    """ApprovalManager usa Lock para thread safety."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")

    handler.pre_approve()

    results = []
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait()
        results.append(handler.approve(tool_name="x", summary="test"))

    t1 = threading.Thread(target=consume)
    t2 = threading.Thread(target=consume)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results.count(True) == 1
    assert results.count(False) == 1


@patch('builtins.print')
def test_pre_approval_handler_prints_pre_approved_status(mock_print):
    """ApprovalManager imprime status quando pré-aprovado."""
    handler = ApprovalManager(None)
    handler.pre_approve()

    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    found = any(
        "pré-aprovado" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


@patch('builtins.print')
def test_pre_approval_handler_delegates_to_base_on_deny(mock_print):
    """Quando não pré-aprovado, delega ao handler interativo."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")

    result = handler.approve(tool_name="shell", summary="ls")
    assert result is False


def test_pre_approval_handler_thread_approve_all_short_circuits_base_without_logging():
    handler = ApprovalManager(None)

    handler.set_thread_approve_all(True, silent=True)
    try:
        result = handler.approve(tool_name="apply_patch", summary="patch")
    finally:
        handler.set_thread_approve_all(False)

    assert result is True


def test_pre_approval_handler_thread_approve_all_is_cleared_after_cycle():
    handler = ApprovalManager(None, input_fn=lambda _: "n")

    handler.set_thread_approve_all(True)
    handler.reset_approve_all_after_cycle()

    result = handler.approve(tool_name="apply_patch", summary="patch")
    assert result is False


def test_pre_approval_handler_scope_approve_all_propagates_across_threads_without_logging():
    handler = ApprovalManager(None)

    handler.set_thread_approve_all(True, scope_key="task:qwen", silent=True)
    result_holder = {}

    def worker():
        previous = handler.bind_thread_approval_scope("task:qwen")
        try:
            result_holder["result"] = handler.approve(tool_name="run_shell", summary="cmd")
        finally:
            handler.bind_thread_approval_scope(previous)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    handler.set_thread_approve_all(False, scope_key="task:qwen")

    assert result_holder["result"] is True


@patch('builtins.print')
def test_pre_approval_handler_thread_approve_all_logs_by_default(mock_print):
    handler = ApprovalManager(None)

    handler.set_thread_approve_all(True)
    try:
        result = handler.approve(tool_name="apply_patch", summary="patch")
    finally:
        handler.set_thread_approve_all(False)

    assert result is True
    found = any(
        "[approve-all]" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


# ── ApprovalManager + spinner callbacks (pre-approval) ──────


def test_pre_approval_handler_spinner_callbacks_when_pre_approved():
    """Quando pré-aprovado, spinner callbacks NÃO são chamados
    (a pré-aprovação consome sem interação)."""
    handler = ApprovalManager(None, input_fn=lambda _: "n")

    suspend_spy = MagicMock()
    resume_spy = MagicMock()
    handler.set_spinner_callbacks(suspend_spy, resume_spy)

    handler.pre_approve()
    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    suspend_spy.assert_not_called()
    resume_spy.assert_not_called()


def test_pre_approval_handler_spinner_callbacks_when_delegating_to_base():
    """Quando delega ao approval interativo, spinner callbacks são chamados."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: "y",
        suspend_fn=lambda: calls.append("suspend_fn"),
        resume_fn=lambda: calls.append("resume_fn"),
    )
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )

    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls


def test_pre_approval_handler_spinner_callbacks_on_base_deny():
    """Quando o approval interativo nega, spinner callbacks são chamados no finally."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: "n",
        suspend_fn=lambda: calls.append("suspend_fn"),
        resume_fn=lambda: calls.append("resume_fn"),
    )
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )

    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls


def test_pre_approval_handler_spinner_callbacks_on_base_eof():
    """Quando o approval interativo recebe EOF, spinner callbacks são chamados no finally."""
    calls = []
    handler = ApprovalManager(None,
        input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
        suspend_fn=lambda: calls.append("suspend_fn"),
        resume_fn=lambda: calls.append("resume_fn"),
    )
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )

    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls


# ── ApprovalManager + renderer + spinner ────────────────────


def test_console_approval_handler_renderer_with_spinner_callbacks():
    """Combinação de renderer + spinner callbacks: ordem correta e
    renderer é usado para exibir o prompt."""
    class FakeRenderer(RendererBase):
        def __init__(self):
            self.calls = []

        def show_approval(self, msg):
            self.calls.append(msg)

        @contextmanager
        def approval_window(self, **_kwargs):
            self.calls.append("approval_window:enter")
            try:
                yield
            finally:
                self.calls.append("approval_window:exit")

    order = []
    renderer = FakeRenderer()
    handler = ApprovalManager(None,
        input_fn=lambda _: order.append("input") or "y",
        renderer=renderer,
    )
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: order.append("suspend_spinner"),
        resume_spinner_fn=lambda: order.append("resume_spinner"),
    )

    with patch('builtins.print') as mock_print:
        result = handler.approve(tool_name="shell", summary="ls")

    assert result is True
    assert len(renderer.calls) >= 1
    assert "Aprovar shell" in renderer.calls[0]
    mock_print.assert_not_called()
    assert order == ["suspend_spinner", "input", "resume_spinner"]


def test_console_approval_handler_textual_xthread_uses_approval_gate():
    """Em thread de background com gate ativo, approval deve emitir pergunta semântica."""
    calls = []

    class FakeGate:
        def is_active(self):
            return True

        def read_approval_in_terminal(self, question, prompt):
            calls.append(("approval", question, prompt))
            return "s"

        def read_input_in_terminal(self, _prompt):
            raise AssertionError("approval não deve usar input genérico")

    handler = ApprovalManager(None, input_gate=FakeGate())
    result = {}

    def run_approval():
        result["approved"] = handler.approve(tool_name="run_shell", summary="risco: shell\ncomando: pwd")

    with patch("builtins.print"):
        thread = threading.Thread(target=run_approval)
        thread.start()
        thread.join(timeout=1)

    assert result["approved"] is True
    assert calls == [
        (
            "approval",
            "\nAprovar run_shell :: risco: shell\ncomando: pwd",
            "  Executar? [y/N/a=todas]: ",
        )
    ]


def test_console_approval_handler_textual_xthread_prefers_approval_gate_even_when_inactive():
    """Aprovação Textual fora do prompt principal não deve cair para input()."""
    calls = []

    class FakeGate:
        def is_active(self):
            return False

        def read_approval_in_terminal(self, question, prompt):
            calls.append((question, prompt))
            return "y"

    handler = ApprovalManager(
        None,
        input_gate=FakeGate(),
        input_fn=lambda _: "n",
    )
    result = {}

    def run_approval():
        result["approved"] = handler.approve(tool_name="run_shell", summary="risco: shell")

    with patch("builtins.print"):
        thread = threading.Thread(target=run_approval)
        thread.start()
        thread.join(timeout=1)

    assert result["approved"] is True
    assert calls == [("\nAprovar run_shell :: risco: shell", "  Executar? [y/N/a=todas]: ")]


def test_console_approval_handler_renderer_window_receives_question_metadata():
    """Fallback com renderer deve abrir approval_window com pergunta completa."""
    class FakeRenderer(RendererBase):
        def __init__(self):
            self.windows = []
            self.messages = []

        def show_approval(self, message):
            self.messages.append(message)

        @contextmanager
        def approval_window(self, **kwargs):
            self.windows.append(kwargs)
            yield

    renderer = FakeRenderer()
    handler = ApprovalManager(None, input_fn=lambda _: "y", renderer=renderer)

    result = handler.approve(tool_name="write_file", summary="risco: write\narquivo: README.md")

    assert result is True
    assert renderer.windows == [
        {
            "title": "Permissão solicitada",
            "metadata": {
                "question": "\nAprovar write_file :: risco: write\narquivo: README.md",
            },
        }
    ]


# ── ApprovalManager + input_gate + cancel_event ─────────────


def test_console_approval_handler_input_gate_with_cancel_pre_set():
    """Com input_gate e cancel_event já setado, approve retorna False sem chamar o gate."""
    cancel_event = threading.Event()
    cancel_event.set()
    mock_gate = MagicMock()
    handler = ApprovalManager(None, input_gate=mock_gate, cancel_event=cancel_event)
    with patch('builtins.print'):
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    mock_gate.assert_not_called()


def test_cli_driver_repl_injects_input_gate_in_driver_repl_mode():
    """No modo --driver-repl, CLI instancia DriverRepl com SimpleInputGate (regressão P5)."""
    import quimera.cli as cli_module

    captured = {}

    class FakeInputGate:
        pass

    class FakeDriverRepl:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        def run(self, one_shot_prompt=None):
            captured["one_shot_prompt"] = one_shot_prompt

    with patch.object(cli_module, "DriverRepl", FakeDriverRepl), \
            patch.object(cli_module, "SimpleInputGate", FakeInputGate), \
            patch("sys.argv", ["quimera", "--driver-repl", "ollama-qwen"]), \
            patch("builtins.print"):
        cli_module.main()

    assert captured["args"][0] == "ollama-qwen"
    assert isinstance(captured["kwargs"]["input_gate"], FakeInputGate)
    assert captured["one_shot_prompt"] is None
