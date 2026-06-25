from pathlib import Path
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import PathPermissionError
from quimera.runtime.approval import ApprovalManager
from quimera.runtime.policy import ToolPolicy
from quimera.tasks.executor import TaskExecutor


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


@pytest.fixture
def approval_handler():
    return MagicMock()


def test_executor_denied(config, approval_handler):
    # Line 58-59 coverage
    """Verifica que Test executor denied."""
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="write_file", arguments={"path": "test.py", "content": "print(1)", "replace_existing": True})
    approval_handler.approve.return_value = False
    result = executor.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_apply_patch_requires_approval(config, approval_handler):
    """Verifica que Test executor apply patch requires approval."""
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="apply_patch", arguments={"patch": "*** Begin Patch\n*** End Patch"})
    approval_handler.approve.return_value = False
    result = executor.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_unexpected_exception(config, approval_handler):
    # Line 64-65 coverage
    """Verifica que Test executor unexpected exception."""
    executor = ToolExecutor(config, approval_handler)
    call = ToolCall(name="list_files", arguments={"path": "/tmp"})
    with patch.object(executor.registry, "get") as mock_get:
        mock_handler = MagicMock(side_effect=Exception("Boom"))
        mock_get.return_value = mock_handler
        result = executor.execute(call)
        assert result.ok is False
        assert "Falha inesperada: Boom" in result.error


def test_executor_registers_interactive_command_tools(config, approval_handler):
    """Verifica que Test executor registers interactive command tools."""
    executor = ToolExecutor(config, approval_handler)
    names = executor.registry.names()
    assert "run_shell_command" not in names
    assert "run_shell" in names
    assert "exec_command" in names
    assert "write_stdin" in names
    assert "close_command_session" in names
    assert "memory_save" in names
    assert "memory_retrieve" in names


def test_executor_normalizes_run_alias_with_commands_list(tmp_path):
    """Verifica que Test executor normalizes run alias with commands list."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.approval_handler.approve.return_value = True
    result = executor.execute(ToolCall(name="run", arguments={"commands": ["echo hello"]}))
    assert result.ok is True
    assert "hello" in result.content


def test_executor_normalizes_execute_command_alias(tmp_path):
    """Verifica que Test executor normalizes execute command alias."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    executor.approval_handler.approve.return_value = True
    result = executor.execute(ToolCall(name="execute_command", arguments={"command": "echo hello"}))
    assert result.ok is True
    assert result.data["status"] in {"running", "completed"}


def test_executor_memory_save_and_retrieve_roundtrip(tmp_path):
    executor = ToolExecutor(
        ToolRuntimeConfig(
            workspace_root=tmp_path,
            memory_file=tmp_path / "state" / "memory.json",
        ),
        MagicMock(),
    )

    save = executor.execute(
        ToolCall(
            name="memory_save",
            arguments={
                "namespace": "workspace",
                "key": "summary",
                "value": {"text": "hello", "tags": ["context", "active"]},
            },
            metadata={"trusted_context": {"agent_name": "codex"}},
        )
    )
    retrieve = executor.execute(
        ToolCall(
            name="memory_retrieve",
            arguments={"namespace": "workspace", "key": "summary"},
        )
    )

    assert save.ok is True
    assert save.data["revision"] == 1
    assert retrieve.ok is True
    assert retrieve.data["revision"] == 1
    assert len(retrieve.data["entries"]) == 1
    entry = retrieve.data["entries"][0]
    assert entry["namespace"] == "workspace"
    assert entry["key"] == "summary"
    assert entry["value"]["text"] == "hello"
    assert entry["tags"] == ["context", "active"]
    assert entry["updated_by"] == "codex"


def test_executor_memory_retrieve_filters_by_prefix_and_tags(tmp_path):
    executor = ToolExecutor(
        ToolRuntimeConfig(
            workspace_root=tmp_path,
            memory_file=tmp_path / "state" / "memory.json",
        ),
        MagicMock(),
    )

    executor.execute(
        ToolCall(
            name="memory_save",
            arguments={
                "namespace": "workspace",
                "key": "decision.api",
                "value": {"text": "v1", "tags": ["decision", "api"]},
            },
        )
    )
    executor.execute(
        ToolCall(
            name="memory_save",
            arguments={
                "namespace": "workspace",
                "key": "decision.ui",
                "value": {"text": "v2", "tags": ["decision", "ui"]},
            },
        )
    )

    retrieve = executor.execute(
        ToolCall(
            name="memory_retrieve",
            arguments={"namespace": "workspace", "prefix": "decision.", "tags": ["api"]},
        )
    )

    assert retrieve.ok is True
    assert [entry["key"] for entry in retrieve.data["entries"]] == ["decision.api"]


def test_task_executor_skips_review_claim_when_agent_is_not_operational(tmp_path):
    """Verifica que Test task executor skips review claim when agent is not operational."""
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


def test_executor_allows_mcp_tool_with_propagated_task_scope(tmp_path):
    """Escopo de task propagado pelo MCP deve autorizar tool mutante."""
    approval = MagicMock()
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), approval)
    executor.approval_manager.set_thread_approve_all(
        True, scope_key="task:cli-agent:1", silent=True
    )

    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(
        name="remove_file",
        arguments={"path": "x.txt", "dry_run": False},
        metadata={"_mcp_state": {"quimera_approval_scope": "task:cli-agent:1"}},
    )
    result = executor.execute(call)

    assert result.ok is True
    approval.approve.assert_not_called()
    assert not (tmp_path / "x.txt").exists()


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

def test_set_spinner_callbacks_injects_into_approval_manager():
    """set_spinner_callbacks injeta no ApprovalManager diretamente."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace_root=Path("/tmp")), input_fn=lambda _: "y")
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    assert handler._console_handler._suspend_spinner_fn[threading.get_ident()] is suspend
    assert handler._console_handler._resume_spinner_fn[threading.get_ident()] is resume


def test_set_spinner_callbacks_ignores_non_console_handler():
    """set_spinner_callbacks não quebra com handler que não tem set_spinner_callbacks."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace_root=Path("/tmp")), input_fn=lambda _: "y")
    handler.set_approve_all(True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)

    suspend = MagicMock()
    resume = MagicMock()
    # Não deve lançar exceção
    executor.set_spinner_callbacks(suspend, resume)


def test_set_approval_cancel_event_injects_into_approval_manager():
    """set_approval_cancel_event injeta cancel_event no ApprovalManager."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace_root=Path("/tmp")), input_fn=lambda _: "y")
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    cancel_event = threading.Event()

    executor.set_approval_cancel_event(cancel_event)

    assert handler._console_handler._cancel_event is cancel_event


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
    handler = ApprovalManager(ToolRuntimeConfig(workspace_root=Path("/tmp")), input_fn=lambda _: "y")
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    assert executor.approval_handler is handler


def test_set_spinner_callbacks_no_op_when_handler_is_none_like():
    """set_spinner_callbacks não quebra com handler sem atributo _base."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace_root=Path("/tmp")), input_fn=lambda _: "y")
    handler.set_approve_all(True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=Path("/tmp")), handler)
    # Já testado, mas reforçando: não deve lançar exceção
    executor.set_spinner_callbacks(MagicMock(), MagicMock())


def test_executor_delegate_dispatches_with_delegation_mode(tmp_path):
    """delegate delega com contrato alinhado ao fluxo de delegation interno."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="delegated ok")
    executor.set_delegate_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "ajuste de bug",
                "context": "arquivo quimera/runtime/executor.py",
            },
        )
    )

    assert result.ok is True
    assert result.content == "delegated ok"
    dispatch.assert_called_once_with(
        "codex",
        delegation={
            "task": "ajuste de bug",
            "context": "arquivo quimera/runtime/executor.py",
        },
        delegation_only=True,
        protocol_mode="delegation",
        primary=False,
        silent=False,
        show_output=False,
        persist_history=True,
        history_snapshot=[],
        max_retries=3,
        progress_callback=None,
    )


def test_executor_delegate_fails_when_dispatch_not_injected(tmp_path):
    """delegate retorna erro explícito quando não há callback de dispatch."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    result = executor.execute(
        ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})
    )

    assert result.ok is False
    assert "not available" in (result.error or "")


def test_executor_delegate_internal_would_not_require_human_approval(tmp_path):
    """delegate interno passa por policy/broker, mas é auto-aprovado dentro do budget."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    call = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})

    assert executor.would_require_approval(call) is False


def test_policy_phase_methods_for_delegate(tmp_path):
    """delegate agora passa por policy/approval broker como risco de delegação."""
    policy = ToolPolicy(ToolRuntimeConfig(workspace_root=tmp_path))
    delegate = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})
    read_file = ToolCall(name="read_file", arguments={"path": "x.txt"})

    assert policy.requires_validation(delegate) is True
    assert policy.requires_path_permission(delegate) is False
    assert policy.requires_approval(delegate) is True

    assert policy.requires_validation(read_file) is True
    assert policy.requires_path_permission(read_file) is True


def test_executor_delegate_goes_through_broker_without_human_prompt_when_internal(tmp_path):
    """delegate não bypassa policy, mas delegação interna é auto-aprovada no broker."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "context": "ctx"},
        )
    )

    assert result.ok is True
    assert result.content == "ok"
    executor.approval_handler.approve.assert_not_called()
    assert executor.approval_broker.audit_log[-1]["event"] == "auto_approved"
    dispatch.assert_called_once()


def test_executor_delegate_rejects_non_string_context(tmp_path):
    """delegate valida `context` localmente mesmo com bypass de policy."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "context": {"invalid": True}},
        )
    )

    assert result.ok is False
    assert "context" in (result.error or "").lower()
    executor.approval_handler.approve.assert_not_called()
    dispatch.assert_not_called()


def test_executor_delegate_uses_fallback_agents_sequentially(tmp_path):
    """delegate tenta fallback em sequência quando alvo principal falha."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    def dispatch(agent_name, **_kwargs):
        if agent_name == "codex":
            return None
        if agent_name == "claude":
            return "ok from claude"
        return None

    spy = MagicMock(side_effect=dispatch)
    executor.set_delegate_fn(spy)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "x",
                "fallback_agents": ["claude", "opencode-qwen3-6-plus-free"],
            },
        )
    )

    assert result.ok is True
    assert result.content == "ok from claude"
    assert [c.args[0] for c in spy.call_args_list] == ["codex", "claude"]


def test_executor_delegate_supports_multiple_sequential_delegations(tmp_path):
    """delegate suporta delegations múltiplos em sequência no mesmo payload."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    def dispatch(agent_name, **kwargs):
        delegation = kwargs.get("delegation") or {}
        return f"{agent_name}:{delegation.get('task')}"

    spy = MagicMock(side_effect=dispatch)
    executor.set_delegate_fn(spy)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "task-1",
                "steps": [
                    {"target_agent": "claude", "request": "task-2"},
                    {"target_agent": "opencode-qwen3-6-plus-free", "request": "task-3"},
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


def test_executor_delegate_rejects_agents_outside_active_pool(tmp_path):
    """delegate deve rejeitar alvos que não estão no pool ativo da sessão."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)
    executor.set_active_agents_provider(lambda: ["codex", "claude"])

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "opencode-big-pickle",
                "request": "x",
                "fallback_agents": ["claude"],
            },
        )
    )

    assert result.ok is False
    assert "not active in current pool" in (result.error or "")
    dispatch.assert_not_called()


def test_executor_delegate_rejects_inactive_agent_between_delegation_steps(tmp_path):
    """delegate rejeita step intermediário quando agente não está mais no pool ativo."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())

    def dispatch(agent_name, **kwargs):
        delegation = kwargs.get("delegation") or {}
        return f"{agent_name}:{delegation.get('task')}"

    spy = MagicMock(side_effect=dispatch)
    executor.set_delegate_fn(spy)

    active = ["codex"]

    def active_provider():
        return list(active)

    executor.set_active_agents_provider(active_provider)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "task-1",
                "steps": [
                    {"target_agent": "opencode-big-pickle", "request": "task-2"},
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


def test_executor_delegate_truncates_long_context_and_task(tmp_path):
    """delegate deve limitar tamanho de task/context para reduzir payload."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace_root=tmp_path), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)

    long_task = "t" * 3000
    long_context = "c" * 8000
    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": long_task,
                "context": long_context,
            },
        )
    )

    assert result.ok is True
    kwargs = dispatch.call_args.kwargs
    delegation = kwargs["delegation"]
    assert len(delegation["task"]) == 1200
    assert len(delegation["context"]) == 4000
