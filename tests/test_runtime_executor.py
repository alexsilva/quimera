from pathlib import Path
import tempfile
import threading
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
    repository = MagicMock()
    repository.claim_task.return_value = None
    executor = TaskExecutor("gemini", db_path=tmp_path / "tasks.db", poll_interval=0, repository=repository)
    executor.set_review_handler(lambda _task: True)
    executor.set_review_eligibility(lambda: False)
    executor._running = True

    def stop_loop(*_args, **_kwargs):
        executor._running = False
        return True

    with patch.object(executor, "_wait_or_stop", side_effect=stop_loop):
        executor._poll_loop()

    repository.claim_review_task.assert_not_called()


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

    assert handler._suspend_spinner_fn[threading.get_ident()] is suspend
    assert handler._resume_spinner_fn[threading.get_ident()] is resume


def test_set_spinner_callbacks_traverses_pre_approval_wrapper():
    """set_spinner_callbacks atravessa PreApprovalHandler e injeta no base."""
    base = ConsoleApprovalHandler()
    pre = PreApprovalHandler(base)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), pre)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    # O base (ConsoleApprovalHandler) recebeu os callbacks
    assert base._suspend_spinner_fn[threading.get_ident()] is suspend
    assert base._resume_spinner_fn[threading.get_ident()] is resume
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


def test_set_approval_cancel_event_injects_into_console_handler():
    """set_approval_cancel_event injeta cancel_event no ConsoleApprovalHandler."""
    handler = ConsoleApprovalHandler()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    cancel_event = threading.Event()

    executor.set_approval_cancel_event(cancel_event)

    assert handler._cancel_event is cancel_event


def test_set_approval_cancel_event_traverses_pre_approval_wrapper():
    """set_approval_cancel_event atravessa PreApprovalHandler e injeta no base."""
    base = ConsoleApprovalHandler()
    pre = PreApprovalHandler(base)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), pre)
    cancel_event = threading.Event()

    executor.set_approval_cancel_event(cancel_event)

    assert base._cancel_event is cancel_event


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
    assert inner_base._suspend_spinner_fn[threading.get_ident()] is suspend
    assert inner_base._resume_spinner_fn[threading.get_ident()] is resume
    # Camadas intermediárias não recebem
    assert not hasattr(middle, "_suspend_spinner_fn")
    assert not hasattr(outer, "_suspend_spinner_fn")


def test_set_spinner_callbacks_no_op_when_handler_is_none_like():
    """set_spinner_callbacks não quebra com handler sem atributo _base."""
    handler = AutoApprovalHandler(approve_all=True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    # Já testado, mas reforçando: não deve lançar exceção
    executor.set_spinner_callbacks(MagicMock(), MagicMock())


def test_executor_call_agent_dispatches_with_handoff_mode(tmp_path):
    """call_agent delega com contrato alinhado ao fluxo de handoff interno."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="delegated ok")
    executor.set_call_agent_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={
                "agent_name": "codex",
                "task": "ajuste de bug",
                "context": "arquivo quimera/runtime/executor.py",
            },
        )
    )

    assert result.ok is True
    assert result.content == "delegated ok"
    dispatch.assert_called_once_with(
        "codex",
        handoff={
            "task": "ajuste de bug",
            "context": "arquivo quimera/runtime/executor.py",
        },
        handoff_only=True,
        protocol_mode="handoff",
        primary=False,
        silent=True,
        show_output=False,
        persist_history=True,
        history_snapshot=[],
        max_retries=1,
    )


def test_executor_call_agent_fails_when_dispatch_not_injected(tmp_path):
    """call_agent retorna erro explícito quando não há callback de dispatch."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    result = executor.execute(
        ToolCall(name="call_agent", arguments={"agent_name": "codex", "task": "x"})
    )

    assert result.ok is False
    assert "not available" in (result.error or "")


def test_executor_call_agent_would_not_require_approval(tmp_path):
    """call_agent não deve ser marcado como exigindo aprovação."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    call = ToolCall(name="call_agent", arguments={"agent_name": "codex", "task": "x"})

    with patch.object(executor.policy, "validate", side_effect=AssertionError("policy should not run")):
        assert executor.would_require_approval(call) is False


def test_policy_phase_methods_for_call_agent(tmp_path):
    """call_agent deve pular validação/path/approval pelo contrato de phases."""
    policy = ToolPolicy(ToolRuntimeConfig(workspace_root=tmp_path))
    call_agent = ToolCall(name="call_agent", arguments={"agent_name": "codex", "task": "x"})
    read_file = ToolCall(name="read_file", arguments={"path": "x.txt"})

    assert policy.requires_validation(call_agent) is False
    assert policy.requires_path_permission(call_agent) is False
    assert policy.requires_approval(call_agent) is False

    assert policy.requires_validation(read_file) is True
    assert policy.requires_path_permission(read_file) is True


def test_executor_call_agent_bypasses_policy_and_approval(tmp_path):
    """call_agent bypassa policy/approval e delega diretamente."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_call_agent_fn(dispatch)

    with patch.object(executor.policy, "validate", side_effect=AssertionError("policy should not run")):
        result = executor.execute(
            ToolCall(
                name="call_agent",
                arguments={"agent_name": "codex", "task": "x", "context": "ctx"},
            )
        )

    assert result.ok is True
    assert result.content == "ok"
    executor.approval_handler.approve.assert_not_called()
    dispatch.assert_called_once()


def test_executor_call_agent_rejects_non_string_context(tmp_path):
    """call_agent valida `context` localmente mesmo com bypass de policy."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_call_agent_fn(dispatch)

    with patch.object(executor.policy, "validate", side_effect=AssertionError("policy should not run")):
        result = executor.execute(
            ToolCall(
                name="call_agent",
                arguments={"agent_name": "codex", "task": "x", "context": {"invalid": True}},
            )
        )

    assert result.ok is False
    assert "context" in (result.error or "").lower()
    executor.approval_handler.approve.assert_not_called()
    dispatch.assert_not_called()


def test_executor_call_agent_uses_fallback_agents_sequentially(tmp_path):
    """call_agent tenta fallback em sequência quando alvo principal falha."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    def dispatch(agent_name, **_kwargs):
        if agent_name == "codex":
            return None
        if agent_name == "claude":
            return "ok from claude"
        return None

    spy = MagicMock(side_effect=dispatch)
    executor.set_call_agent_fn(spy)

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={
                "agent_name": "codex",
                "task": "x",
                "fallback_agents": ["claude", "opencode-qwen3-6-plus-free"],
            },
        )
    )

    assert result.ok is True
    assert result.content == "ok from claude"
    assert [c.args[0] for c in spy.call_args_list] == ["codex", "claude"]


def test_executor_call_agent_supports_multiple_sequential_handoffs(tmp_path):
    """call_agent suporta handoffs múltiplos em sequência no mesmo payload."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    def dispatch(agent_name, **kwargs):
        handoff = kwargs.get("handoff") or {}
        return f"{agent_name}:{handoff.get('task')}"

    spy = MagicMock(side_effect=dispatch)
    executor.set_call_agent_fn(spy)

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={
                "agent_name": "codex",
                "task": "task-1",
                "handoffs": [
                    {"agent_name": "claude", "task": "task-2"},
                    {"agent_name": "opencode-qwen3-6-plus-free", "task": "task-3"},
                ],
            },
        )
    )

    assert result.ok is True
    assert "[codex] codex:task-1" in (result.content or "")
    assert "[claude] claude:task-2" in (result.content or "")
    assert "[opencode-qwen3-6-plus-free] opencode-qwen3-6-plus-free:task-3" in (result.content or "")
    assert [c.args[0] for c in spy.call_args_list] == [
        "codex",
        "claude",
        "opencode-qwen3-6-plus-free",
    ]


def test_executor_call_agent_rejects_agents_outside_active_pool(tmp_path):
    """call_agent deve rejeitar alvos que não estão no pool ativo da sessão."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_call_agent_fn(dispatch)
    executor.set_active_agents_provider(lambda: ["codex", "claude"])

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={
                "agent_name": "opencode-big-pickle",
                "task": "x",
                "fallback_agents": ["claude"],
            },
        )
    )

    assert result.ok is False
    assert "not active in current pool" in (result.error or "")
    dispatch.assert_not_called()


def test_executor_call_agent_rejects_inactive_agent_between_handoff_steps(tmp_path):
    """call_agent rejeita step intermediário quando agente não está mais no pool ativo."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    def dispatch(agent_name, **kwargs):
        handoff = kwargs.get("handoff") or {}
        return f"{agent_name}:{handoff.get('task')}"

    spy = MagicMock(side_effect=dispatch)
    executor.set_call_agent_fn(spy)

    active = ["codex"]

    def active_provider():
        return list(active)

    executor.set_active_agents_provider(active_provider)

    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={
                "agent_name": "codex",
                "task": "task-1",
                "handoffs": [
                    {"agent_name": "opencode-big-pickle", "task": "task-2"},
                ],
            },
        )
    )

    assert result.ok is False
    assert "not active in current pool" in (result.error or "")
    assert "opencode-big-pickle" in (result.error or "")
    # First step should have been dispatched, second should not
    assert spy.call_count == 1
    assert spy.call_args[0][0] == "codex"


def test_executor_call_agent_truncates_long_context_and_task(tmp_path):
    """call_agent deve limitar tamanho de task/context para reduzir payload."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_call_agent_fn(dispatch)

    long_task = "t" * 3000
    long_context = "c" * 8000
    result = executor.execute(
        ToolCall(
            name="call_agent",
            arguments={
                "agent_name": "codex",
                "task": long_task,
                "context": long_context,
            },
        )
    )

    assert result.ok is True
    kwargs = dispatch.call_args.kwargs
    handoff = kwargs["handoff"]
    assert len(handoff["task"]) == 1200
    assert len(handoff["context"]) == 4000
