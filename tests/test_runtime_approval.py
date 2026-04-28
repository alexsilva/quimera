"""Testes unitários para o módulo quimera.runtime.approval.

Cobre todas as classes e fluxos: ConsoleApprovalHandler, AutoApprovalHandler,
PreApprovalHandler, NonBlockingConsoleApprovalHandler, e suas interações
com suspend/resume/spinner callbacks.
"""
from unittest.mock import MagicMock, patch, call

import pytest

from quimera.runtime.approval import (
    ApprovalHandler,
    ConsoleApprovalHandler,
    AutoApprovalHandler,
    PreApprovalHandler,
    NonBlockingConsoleApprovalHandler,
)


# ── ApprovalHandler (abstract) ──────────────────────────────

def test_approval_handler_abstract():
    """Classe base levanta NotImplementedError."""
    class ConcreteHandler(ApprovalHandler):
        def approve(self, *, tool_name: str, summary: str) -> bool:
            return super().approve(tool_name=tool_name, summary=summary)

    handler = ConcreteHandler()
    with pytest.raises(NotImplementedError):
        handler.approve(tool_name="test", summary="test")


# ── ConsoleApprovalHandler ──────────────────────────────────


@patch('builtins.input')
@patch('builtins.print')
def test_console_approval_handler_yes(mock_print, mock_input):
    """Resposta 'y' aprova."""
    handler = ConsoleApprovalHandler()
    mock_input.return_value = "y"
    assert handler.approve(tool_name="shell", summary="ls") is True
    mock_print.assert_called()


@patch('builtins.input')
def test_console_approval_handler_no(mock_input):
    """Resposta 'n' nega."""
    handler = ConsoleApprovalHandler()
    mock_input.return_value = "n"
    assert handler.approve(tool_name="shell", summary="rm -rf /") is False


@patch('builtins.input')
def test_console_approval_handler_empty(mock_input):
    """Resposta vazia nega."""
    handler = ConsoleApprovalHandler()
    mock_input.return_value = ""
    assert handler.approve(tool_name="shell", summary="echo") is False


def test_console_approval_handler_with_custom_input_fn():
    """Quando input_fn é injetada, deve usá-la em vez de input() builtin."""
    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "yes"

    handler = ConsoleApprovalHandler(input_fn=fake_input)
    result = handler.approve(tool_name="shell", summary="ls")

    assert result is True
    assert len(calls) == 1
    assert "Executar?" in calls[0]
    assert "y/N" in calls[0]


def test_console_approval_handler_accepts_sim():
    """Aceita 'sim' (português) como resposta afirmativa."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: "sim")
    assert handler.approve(tool_name="shell", summary="ls") is True


def test_console_approval_handler_accepts_s():
    """Aceita 's' como resposta afirmativa."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: "s")
    assert handler.approve(tool_name="shell", summary="ls") is True


def test_console_approval_handler_accepts_yes():
    """Aceita 'yes' como resposta afirmativa."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: "yes")
    assert handler.approve(tool_name="shell", summary="ls") is True


def test_console_approval_handler_rejects_uppercase_n():
    """'N' (maiúsculo) nega."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: "N")
    assert handler.approve(tool_name="shell", summary="ls") is False


def test_console_approval_handler_eof_error_returns_false():
    """EOFError (stdin fechado) retorna False."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: (_ for _ in ()).throw(EOFError()))
    with patch('builtins.print'):
        assert handler.approve(tool_name="shell", summary="ls") is False


# ── ConsoleApprovalHandler + suspend/resume ─────────────────


def test_console_approval_handler_suspend_resume_called():
    """suspend_fn e resume_fn são chamadas durante a aprovação."""
    calls = []
    handler = ConsoleApprovalHandler(
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
    handler = ConsoleApprovalHandler(
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
    handler = ConsoleApprovalHandler(
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

    handler = ConsoleApprovalHandler(
        input_fn=input_fn,
        suspend_fn=suspend_fn,
        resume_fn=resume_fn,
    )
    handler.approve(tool_name="shell", summary="ls")
    assert call_order == ["suspend", "input", "resume"]


# ── ConsoleApprovalHandler + spinner callbacks ──────────────


def test_console_approval_handler_spinner_callbacks_called():
    """Spinner callbacks são chamados na ordem: suspend_spinner → input → resume_spinner → resume."""
    calls = []
    handler = ConsoleApprovalHandler(input_fn=lambda _: "y")
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
    handler = ConsoleApprovalHandler(
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
    handler = ConsoleApprovalHandler(input_fn=lambda _: "n")
    handler.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    handler.approve(tool_name="shell", summary="ls")
    assert calls == ["suspend_spinner", "resume_spinner"]


def test_console_approval_handler_spinner_callbacks_not_set():
    """Sem spinner callbacks, nada quebra."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: "y")
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    # Não deve lançar AttributeError


def test_console_approval_handler_spinner_and_suspend_order():
    """Ordem completa: suspend_fn → suspend_spinner → input → resume_spinner → resume_fn."""
    order = []
    handler = ConsoleApprovalHandler(
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


# ── ConsoleApprovalHandler + renderer ───────────────────────


def test_console_approval_handler_with_renderer():
    """Quando um renderer é injetado, usa show_system em vez de print."""
    class FakeRenderer:
        def __init__(self):
            self.calls = []

        def show_system(self, msg):
            self.calls.append(msg)

    renderer = FakeRenderer()
    handler = ConsoleApprovalHandler(input_fn=lambda _: "y", renderer=renderer)
    with patch('builtins.print') as mock_print:
        result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    # renderer.show_system foi chamado, print não
    assert len(renderer.calls) >= 1
    assert "[aprovação]" in renderer.calls[0]


def test_console_approval_handler_renderer_shows_eof_message():
    """Com renderer, mensagem de EOF também usa show_system."""
    class FakeRenderer:
        def __init__(self):
            self.calls = []

        def show_system(self, msg):
            self.calls.append(msg)

    renderer = FakeRenderer()
    handler = ConsoleApprovalHandler(
        input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
        renderer=renderer,
    )
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is False
    assert any("stdin não disponível" in m for m in renderer.calls)


def test_console_approval_handler_no_renderer_uses_print():
    """Sem renderer, usa print() builtin."""
    handler = ConsoleApprovalHandler(input_fn=lambda _: "y")
    with patch('builtins.print') as mock_print:
        handler.approve(tool_name="shell", summary="ls")
    mock_print.assert_called()
    # Pelo menos uma chamada tem "[aprovação]"
    found = any(
        "[aprovação]" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


# ── ConsoleApprovalHandler + None input_fn usa builtins.input ──


@patch('builtins.input')
@patch('builtins.print')
def test_console_approval_handler_none_input_fn_uses_builtin(mock_print, mock_input):
    """Quando input_fn=None no construtor, usa input() builtin dinamicamente."""
    handler = ConsoleApprovalHandler(input_fn=None)
    mock_input.return_value = "y"
    result = handler.approve(tool_name="shell", summary="ls")
    assert result is True
    mock_input.assert_called_once()


# ── AutoApprovalHandler ─────────────────────────────────────


def test_auto_approval_handler_true():
    """AutoApprovalHandler com approve_all=True sempre aprova."""
    handler = AutoApprovalHandler(approve_all=True)
    assert handler.approve(tool_name="shell", summary="ls") is True
    assert handler.approve(tool_name="write_file", summary="danger") is True


def test_auto_approval_handler_false():
    """AutoApprovalHandler com approve_all=False sempre nega."""
    handler = AutoApprovalHandler(approve_all=False)
    assert handler.approve(tool_name="shell", summary="ls") is False
    assert handler.approve(tool_name="write_file", summary="danger") is False


@patch('builtins.print')
def test_auto_approval_handler_prints_status(mock_print):
    """AutoApprovalHandler imprime status de auto-aprovação."""
    handler = AutoApprovalHandler(approve_all=True)
    handler.approve(tool_name="shell", summary="ls")
    found = any(
        "auto-aprovado" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


# ── PreApprovalHandler ──────────────────────────────────────


def test_pre_approval_handler_delegates_when_not_pre_approved():
    """Sem pré-aprovação, delega ao handler base."""
    base_approval_handler = ConsoleApprovalHandler(input_fn=lambda _: "y")
    pre = PreApprovalHandler(base_approval_handler)
    with patch('builtins.print'):
        result = pre.approve(tool_name="shell", summary="ls")
    assert result is True  # base retorna True porque input_fn retorna "y"


def test_pre_approval_handler_consumes_pre_approval():
    """Pré-aprovação é consumida uma única vez e depois resetada."""
    base = ConsoleApprovalHandler(input_fn=lambda _: "n")  # base nega
    pre = PreApprovalHandler(base)
    pre.pre_approve()
    with patch('builtins.print'):
        # Primeira chamada: pré-aprovada → True
        assert pre.approve(tool_name="shell", summary="ls") is True
        # Segunda chamada: pré-aprovação já foi consumida → delega ao base → False
        assert pre.approve(tool_name="shell", summary="ls") is False


def test_pre_approval_handler_reset():
    """Reset descarta a pré-aprovação sem consumir."""
    base = ConsoleApprovalHandler(input_fn=lambda _: "n")
    pre = PreApprovalHandler(base)
    pre.pre_approve()
    pre.reset()
    with patch('builtins.print'):
        assert pre.approve(tool_name="shell", summary="ls") is False


def test_pre_approval_handler_multiple_pre_approvals():
    """Cada pre_approve() é consumida individualmente."""
    base = ConsoleApprovalHandler(input_fn=lambda _: "n")
    pre = PreApprovalHandler(base)
    with patch('builtins.print'):
        pre.pre_approve()
        assert pre.approve(tool_name="a", summary="1") is True   # consome
        assert pre.approve(tool_name="b", summary="2") is False  # sem pré-aprovação
        pre.pre_approve()
        pre.pre_approve()  # duas pré-aprovações? A segunda sobrescreve a primeira
        assert pre.approve(tool_name="c", summary="3") is True   # consome
        assert pre.approve(tool_name="d", summary="4") is False  # acabou


def test_pre_approval_handler_is_thread_safe():
    """PreApprovalHandler usa Lock para thread safety."""
    import threading
    base = MagicMock()
    base.approve.return_value = False
    pre = PreApprovalHandler(base)

    pre.pre_approve()

    # Simula acesso concorrente em threads
    results = []
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait()
        results.append(pre.approve(tool_name="x", summary="test"))

    t1 = threading.Thread(target=consume)
    t2 = threading.Thread(target=consume)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Apenas uma thread deve consumir a pré-aprovação
    assert results.count(True) == 1
    assert results.count(False) == 1


@patch('builtins.print')
def test_pre_approval_handler_prints_pre_approved_status(mock_print):
    """PreApprovalHandler imprime status quando pré-aprovado."""
    base = MagicMock()
    base.approve.return_value = False
    pre = PreApprovalHandler(base)
    pre.pre_approve()

    result = pre.approve(tool_name="shell", summary="ls")
    assert result is True
    # Não deve delegar ao base
    base.approve.assert_not_called()
    # Deve imprimir status
    found = any(
        "pré-aprovado" in str(call_args)
        for call_args in mock_print.call_args_list
    )
    assert found


@patch('builtins.print')
def test_pre_approval_handler_delegates_to_base_on_deny(mock_print):
    """Quando não pré-aprovado, delega ao base."""
    base = MagicMock()
    base.approve.return_value = False
    pre = PreApprovalHandler(base)

    result = pre.approve(tool_name="shell", summary="ls")
    assert result is False
    base.approve.assert_called_once_with(tool_name="shell", summary="ls")


# ── NonBlockingConsoleApprovalHandler ───────────────────────


@patch('builtins.print')
def test_nonblocking_console_approval_accepts_sim(mock_print):
    """NonBlockingConsoleApprovalHandler aceita 'sim'."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    with patch.object(handler, '_read_with_timeout', return_value="sim"):
        assert handler.approve(tool_name="shell", summary="ls") is True


@patch('builtins.print')
def test_nonblocking_console_approval_timeout(mock_print):
    """Timeout retorna False."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    with patch.object(handler, '_read_with_timeout', return_value=None):
        assert handler.approve(tool_name="shell", summary="ls") is False


@patch('builtins.print')
def test_nonblocking_console_approval_no(mock_print):
    """Resposta 'n' retorna False."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    with patch.object(handler, '_read_with_timeout', return_value="n"):
        assert handler.approve(tool_name="shell", summary="ls") is False


@patch('builtins.print')
def test_nonblocking_console_approval_yes(mock_print):
    """Resposta 'yes' retorna True."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    with patch.object(handler, '_read_with_timeout', return_value="yes"):
        assert handler.approve(tool_name="shell", summary="ls") is True


@patch('builtins.print')
def test_nonblocking_console_approval_accepts_s(mock_print):
    """NonBlockingConsoleApprovalHandler aceita 's'."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    with patch.object(handler, '_read_with_timeout', return_value="s"):
        assert handler.approve(tool_name="shell", summary="ls") is True


@patch('builtins.print')
def test_nonblocking_console_approval_custom_timeout(mock_print):
    """NonBlockingConsoleApprovalHandler respeita timeout customizado."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=1.5)
    assert handler._timeout == 1.5


# ── NonBlockingConsoleApprovalHandler._read_with_timeout ────


@patch('builtins.print')
@patch('sys.stdin')
@patch('select.select')
def test_nonblocking_read_with_timeout_stdin_none(mock_select, mock_stdin, mock_print):
    """_read_with_timeout retorna None quando sys.stdin é None."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    mock_stdin.__bool__.return_value = False  # faz "if stdin is None" ser True
    # Na verdade o código usa "stdin = sys.stdin; if stdin is None"
    # Precisamos que sys.stdin seja avaliado como None
    import sys
    with patch.object(sys, 'stdin', None):
        result = handler._read_with_timeout(5.0)
    assert result is None


@patch('builtins.print')
@patch('select.select')
def test_nonblocking_read_with_timeout_no_data(mock_select, mock_print):
    """_read_with_timeout retorna None quando select retorna lista vazia."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    mock_select.return_value = ([], [], [])
    result = handler._read_with_timeout(0.1)
    assert result is None


@patch('builtins.print')
@patch('sys.stdin')
@patch('select.select')
def test_nonblocking_read_with_timeout_has_data(mock_select, mock_stdin, mock_print):
    """_read_with_timeout retorna dados quando select tem ready fd."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    mock_select.return_value = ([mock_stdin], [], [])
    mock_stdin.readline.return_value = "y\n"
    result = handler._read_with_timeout(5.0)
    assert result == "y\n"


@patch('builtins.print')
@patch('select.select', side_effect=OSError("termios fail"))
def test_nonblocking_read_with_timeout_exception_returns_none(mock_select, mock_print):
    """_read_with_timeout retorna None em caso de exceção."""
    handler = NonBlockingConsoleApprovalHandler(timeout_seconds=5.0)
    result = handler._read_with_timeout(0.1)
    assert result is None


# ── PreApprovalHandler + spinner callbacks ──────────────────


def test_pre_approval_handler_spinner_callbacks_when_pre_approved():
    """Quando pré-aprovado, spinner callbacks NÃO são chamados
    (a pré-aprovação consome sem interação)."""
    base = ConsoleApprovalHandler(input_fn=lambda _: "n")
    pre = PreApprovalHandler(base)

    suspend_spy = MagicMock()
    resume_spy = MagicMock()
    base.set_spinner_callbacks(suspend_spy, resume_spy)

    pre.pre_approve()
    with patch('builtins.print'):
        result = pre.approve(tool_name="shell", summary="ls")
    assert result is True
    # Spinner callbacks não devem ser chamados porque não houve interação
    suspend_spy.assert_not_called()
    resume_spy.assert_not_called()


def test_pre_approval_handler_spinner_callbacks_when_delegating_to_base():
    """Quando delega ao base (ConsoleApprovalHandler), spinner callbacks
    são chamados pelo base durante _approve_interactive."""
    calls = []
    base = ConsoleApprovalHandler(
        input_fn=lambda _: "y",
        suspend_fn=lambda: calls.append("suspend_fn"),
        resume_fn=lambda: calls.append("resume_fn"),
    )
    base.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    pre = PreApprovalHandler(base)

    with patch('builtins.print'):
        result = pre.approve(tool_name="shell", summary="ls")
    assert result is True
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls


def test_pre_approval_handler_spinner_callbacks_on_base_deny():
    """Quando delega e o base nega, spinner callbacks são chamados no finally."""
    calls = []
    base = ConsoleApprovalHandler(
        input_fn=lambda _: "n",
        suspend_fn=lambda: calls.append("suspend_fn"),
        resume_fn=lambda: calls.append("resume_fn"),
    )
    base.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    pre = PreApprovalHandler(base)

    with patch('builtins.print'):
        result = pre.approve(tool_name="shell", summary="ls")
    assert result is False
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls


def test_pre_approval_handler_spinner_callbacks_on_base_eof():
    """Quando delega e o base recebe EOF, spinner callbacks são chamados no finally."""
    calls = []
    base = ConsoleApprovalHandler(
        input_fn=lambda _: (_ for _ in ()).throw(EOFError()),
        suspend_fn=lambda: calls.append("suspend_fn"),
        resume_fn=lambda: calls.append("resume_fn"),
    )
    base.set_spinner_callbacks(
        suspend_spinner_fn=lambda: calls.append("suspend_spinner"),
        resume_spinner_fn=lambda: calls.append("resume_spinner"),
    )
    pre = PreApprovalHandler(base)

    with patch('builtins.print'):
        result = pre.approve(tool_name="shell", summary="ls")
    assert result is False
    assert "suspend_spinner" in calls
    assert "resume_spinner" in calls


# ── ConsoleApprovalHandler + renderer + spinner ─────────────


def test_console_approval_handler_renderer_with_spinner_callbacks():
    """Combinação de renderer + spinner callbacks: ordem correta e
    renderer é usado para exibir o prompt."""
    class FakeRenderer:
        def __init__(self):
            self.calls = []

        def show_system(self, msg):
            self.calls.append(msg)

    order = []
    renderer = FakeRenderer()
    handler = ConsoleApprovalHandler(
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
    # renderer.show_system foi usado
    assert len(renderer.calls) >= 1
    assert "[aprovação]" in renderer.calls[0]
    # print NÃO foi chamado
    mock_print.assert_not_called()
    # Ordem correta
    assert order == ["suspend_spinner", "input", "resume_spinner"]

