from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import PathPermissionError
from quimera.runtime.approval import (
    ConsoleApprovalHandler,
    PreApprovalHandler,
    AutoApprovalHandler,
)
from quimera.runtime.policy import ToolPolicy
from quimera.runtime.task_executor import TaskExecutor


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


@pytest.fixture
def approval_handler():
    return MagicMock()


def test_executor_denied(config, approval_handler):
    # Line 58-59 coverage
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="write_file", arguments={"path": "test.py", "content": "print(1)"})
    approval_handler.approve.return_value = False
    result = executor.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_apply_patch_requires_approval(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="apply_patch", arguments={"patch": "*** Begin Patch\n*** End Patch"})
    approval_handler.approve.return_value = False
    result = executor.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_unexpected_exception(config, approval_handler):
    # Line 64-65 coverage
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="list_files", arguments={"path": "/tmp"})
    with patch.object(executor.registry, "get") as mock_get:
        mock_handler = MagicMock(side_effect=Exception("Boom"))
        mock_get.return_value = mock_handler
        result = executor.execute(call)
        assert result.ok is False
        assert "Falha inesperada: Boom" in result.error


def test_maybe_execute_from_response_parse_error(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    response = '<tool function="read_file" arguments="{invalid}" />'
    text, result = executor.maybe_execute_from_response(response)
    assert result.ok is False
    assert result.tool_name == "parse"


def test_maybe_execute_from_response_none(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    text, result = executor.maybe_execute_from_response("no tool")
    assert result is None


def test_executor_registers_interactive_command_tools(config, approval_handler):
    executor = ToolExecutor(config, approval_handler)
    names = executor.registry.names()
    assert "run_shell_command" not in names
    assert "run_shell" in names
    assert "exec_command" in names
    assert "write_stdin" in names
    assert "close_command_session" in names


def test_executor_normalizes_run_alias_with_commands_list(tmp_path):
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.approval_handler.approve.return_value = True
    result = executor.execute(ToolCall(name="run", arguments={"commands": ["echo hello"]}))
    assert result.ok is True
    assert "hello" in result.content


def test_executor_normalizes_execute_command_alias(tmp_path):
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.approval_handler.approve.return_value = True
    result = executor.execute(ToolCall(name="execute_command", arguments={"command": "echo hello"}))
    assert result.ok is True
    assert result.data["status"] == "completed"


def test_task_executor_skips_review_claim_when_agent_is_not_operational(tmp_path):
    executor = TaskExecutor("gemini", db_path=tmp_path / "tasks.db", poll_interval=0)
    executor.set_review_handler(lambda _task: True)
    executor.set_review_eligibility(lambda: False)
    executor._running = True

    def stop_loop(*_args, **_kwargs):
        executor._running = False
        return True

    with patch("quimera.runtime.task_executor.claim_task", return_value=None), patch(
            "quimera.runtime.task_executor.claim_review_task"
    ) as claim_review_task, patch.object(executor, "_wait_or_stop", side_effect=stop_loop):
        executor._poll_loop()

    claim_review_task.assert_not_called()


# ── remove_file executor ─────────────────────────────────────

def test_executor_remove_file_is_registered(config, approval_handler):
    """remove_file está registrado no executor."""
    executor = ToolExecutor(config, approval_handler)
    assert "remove_file" in executor.registry.names()


def test_executor_remove_file_denied_by_approval(tmp_path):
    """remove_file com aprovação negada retorna erro."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=tmp_path),
        MagicMock(),
    )
    executor.approval_handler.approve.return_value = False

    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    result = executor.execute(call)

    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_remove_file_allowed_and_executes(tmp_path):
    """remove_file com aprovação concedida executa e remove o arquivo."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=tmp_path),
        MagicMock(),
    )
    executor.approval_handler.approve.return_value = True

    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    result = executor.execute(call)

    assert result.ok is True
    assert "removido" in result.content.lower()
    assert not (tmp_path / "x.txt").exists()


def test_executor_remove_file_policy_blocks_missing_dry_run(tmp_path):
    """Política bloqueia remove_file sem dry_run=False explícito."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=tmp_path),
        MagicMock(),
    )
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt"})
    result = executor.execute(call)

    assert result.ok is False
    assert "dry_run=False" in result.error


# ── set_spinner_callbacks ───────────────────────────────────

def test_set_spinner_callbacks_injects_into_console_handler():
    """set_spinner_callbacks injeta no ConsoleApprovalHandler diretamente."""
    handler = ConsoleApprovalHandler()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    assert handler._suspend_spinner_fn is suspend
    assert handler._resume_spinner_fn is resume


def test_set_spinner_callbacks_traverses_pre_approval_wrapper():
    """set_spinner_callbacks atravessa PreApprovalHandler e injeta no base."""
    base = ConsoleApprovalHandler()
    pre = PreApprovalHandler(base)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), pre)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    # O base (ConsoleApprovalHandler) recebeu os callbacks
    assert base._suspend_spinner_fn is suspend
    assert base._resume_spinner_fn is resume
    # O PreApprovalHandler não tem os callbacks diretamente
    assert not hasattr(pre, '_suspend_spinner_fn')


def test_set_spinner_callbacks_ignores_non_console_handler():
    """set_spinner_callbacks não quebra com handler que não tem set_spinner_callbacks."""
    handler = AutoApprovalHandler(approve_all=True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)

    suspend = MagicMock()
    resume = MagicMock()
    # Não deve lançar exceção
    executor.set_spinner_callbacks(suspend, resume)


# ── Fluxo unificado de aprovação ────────────────────────────

def test_executor_permission_error_triggers_approval(config, approval_handler):
    """Quando há permission_error, o handler de aprovação é chamado com
    summary contendo 'Permissão necessária'."""
    executor = ToolExecutor(config, approval_handler)
    approval_handler.approve.return_value = False

    permission_error = PathPermissionError("/etc/passwd", Path("/etc/passwd"))

    call = ToolCall(name="list_files", arguments={"path": "."})
    with patch.object(executor.policy, "validate"), \
         patch.object(executor.policy, "check_path_permission", return_value=permission_error):
        result = executor.execute(call)

    assert result.ok is False
    assert "Execução negada" in result.error
    approval_handler.approve.assert_called_once()
    call_kwargs = approval_handler.approve.call_args.kwargs
    assert "Permissão necessária" in call_kwargs["summary"]


def test_executor_permission_error_approved_executes(config, approval_handler):
    """Quando permission_error é aprovado, a ferramenta executa normalmente."""
    executor = ToolExecutor(config, approval_handler)
    approval_handler.approve.return_value = True

    permission_error = PathPermissionError("/etc/passwd", Path("/etc/passwd"))

    call = ToolCall(name="list_files", arguments={"path": "."})
    with patch.object(executor.policy, "validate"), \
         patch.object(executor.policy, "check_path_permission", return_value=permission_error):
        result = executor.execute(call)

    assert result.ok is True


def test_executor_needs_approval_and_permission_error_unified(tmp_path):
    """Quando uma ferramenta tem ambos needs_approval e permission_error,
    o approve é chamado uma única vez com summary de permissão (priority)."""
    config = ToolRuntimeConfig(
        workspace_root=tmp_path,
        require_approval_for_mutations=True,
    )
    approval_handler = MagicMock()
    approval_handler.approve.return_value = False
    executor = ToolExecutor(config, approval_handler)

    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})

    permission_error = PathPermissionError("x.txt", (tmp_path / "x.txt").resolve())
    with patch.object(executor.policy, "check_path_permission", return_value=permission_error):
        result = executor.execute(call)

    assert result.ok is False
    assert approval_handler.approve.call_count == 1
    call_kwargs = approval_handler.approve.call_args.kwargs
    assert "Permissão necessária" in call_kwargs["summary"]


# ── write_stdin na lista de aprovação ──────────────────────

def test_executor_write_stdin_requires_approval_when_mutations_enabled():
    """write_stdin requer aprovação quando require_approval_for_mutations=True."""
    config = ToolRuntimeConfig(
        workspace_root=Path("/tmp"),
        require_approval_for_mutations=True,
    )
    approval_handler = MagicMock()
    approval_handler.approve.return_value = False
    executor = ToolExecutor(config, approval_handler)

    call = ToolCall(name="write_stdin", arguments={"session_id": 1, "chars": "y"})
    result = executor.execute(call)

    assert result.ok is False
    assert "Execução negada" in result.error
    approval_handler.approve.assert_called_once()


def test_executor_write_stdin_no_approval_when_mutations_disabled():
    """write_stdin NÃO requer aprovação quando require_approval_for_mutations=False."""
    config = ToolRuntimeConfig(
        workspace_root=Path("/tmp"),
        require_approval_for_mutations=False,
    )
    approval_handler = MagicMock()
    executor = ToolExecutor(config, approval_handler)

    call = ToolCall(name="write_stdin", arguments={"session_id": 1, "chars": ""})
    result = executor.execute(call)

    # Não deve chamar approve (mas a ferramenta pode falhar por sessão inexistente)
    approval_handler.approve.assert_not_called()


# ── approval_handler property ───────────────────────────────

def test_executor_approval_handler_property():
    """A property approval_handler retorna o handler configurado."""
    handler = ConsoleApprovalHandler()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    assert executor.approval_handler is handler


# ── maybe_execute_from_response (linhas 128-129) ────────────


def test_maybe_execute_from_response_with_tool_call(config, approval_handler):
    """maybe_execute_from_response com tool call válido executa e retorna (response, result)."""
    executor = ToolExecutor(config, approval_handler)
    response = '<tool function="list_files">\n{"path": "/tmp"}\n</tool>'
    text, result = executor.maybe_execute_from_response(response)
    assert text == response
    assert result is not None
    assert result.ok is True
    assert result.tool_name == "list_files"


def test_maybe_execute_from_response_with_tool_call_needs_approval_denied(config, approval_handler):
    """maybe_execute_from_response com tool que requer aprovação negada retorna erro."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=Path("/tmp"), require_approval_for_mutations=True),
        approval_handler,
    )
    approval_handler.approve.return_value = False
    response = '<tool function="write_file">\n{"path": "test.txt", "content": "x"}\n</tool>'
    text, result = executor.maybe_execute_from_response(response)
    assert text == response
    assert result is not None
    assert result.ok is False
    assert "Execução negada" in result.error


def test_maybe_execute_from_response_with_tool_call_approved(config, approval_handler):
    """maybe_execute_from_response com tool aprovada executa com sucesso."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace_root=Path("/tmp"), require_approval_for_mutations=True),
        approval_handler,
    )
    approval_handler.approve.return_value = True
    response = '<tool function="list_files">\n{"path": "/tmp"}\n</tool>'
    text, result = executor.maybe_execute_from_response(response)
    assert text == response
    assert result is not None
    assert result.ok is True


# ── set_spinner_callbacks edge cases ────────────────────────


def test_set_spinner_callbacks_on_pre_approval_with_mock_base():
    """set_spinner_callbacks atravessa PreApprovalHandler com base que
    tem set_spinner_callbacks mas não é ConsoleApprovalHandler."""
    class BaseWithSetter:
        def __init__(self):
            self.set_spinner_callbacks = MagicMock()

    base = BaseWithSetter()
    pre = PreApprovalHandler(base)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), pre)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    # O loop while deve parar no base (que tem set_spinner_callbacks)
    base.set_spinner_callbacks.assert_called_once_with(suspend, resume)


def test_set_spinner_callbacks_double_wrapped_pre_approval():
    """set_spinner_callbacks atravessa dois PreApprovalHandlers."""
    inner_base = ConsoleApprovalHandler()
    middle = PreApprovalHandler(inner_base)
    outer = PreApprovalHandler(middle)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), outer)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    # inner_base (ConsoleApprovalHandler) deve receber os callbacks
    assert inner_base._suspend_spinner_fn is suspend
    assert inner_base._resume_spinner_fn is resume
    # Camadas intermediárias não recebem
    assert not hasattr(middle, "_suspend_spinner_fn")
    assert not hasattr(outer, "_suspend_spinner_fn")


def test_set_spinner_callbacks_no_op_when_handler_is_none_like():
    """set_spinner_callbacks não quebra com handler sem atributo _base."""
    handler = AutoApprovalHandler(approve_all=True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    # Já testado, mas reforçando: não deve lançar exceção
    executor.set_spinner_callbacks(MagicMock(), MagicMock())
