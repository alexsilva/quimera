from pathlib import Path
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.workspace import Workspace
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import PathPermissionError
from quimera.runtime.approval import ApprovalManager
from quimera.runtime.policy import ToolPolicy
from quimera.tasks.executor import TaskExecutor


# ════════════════════════════════════════════════════════════════════════
# Tests for tool denial/approval
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tool_name,arguments", [
    ("write_file", {"path": "test.py", "content": "print(1)", "replace_existing": True}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}),
])
def test_executor_tool_denied_by_approval(executor_with_approval, tool_name, arguments):
    """Verifica que ferramentas mutantes são negadas quando aprovação retorna False."""
    executor_with_approval.approval_handler.approve.return_value = False
    call = ToolCall(name=tool_name, arguments=arguments)
    result = executor_with_approval.execute(call)
    assert result.ok is False
    assert "Execução negada" in result.error


def test_executor_unexpected_exception(executor_with_approval):
    """Verifica que exceção inesperada no handler retorna erro."""
    call = ToolCall(name="list_files", arguments={"path": "."})
    with patch.object(executor_with_approval.registry, "get") as mock_get:
        mock_handler = MagicMock(side_effect=Exception("Boom"))
        mock_get.return_value = mock_handler
        result = executor_with_approval.execute(call)
        assert result.ok is False
        assert "Falha inesperada: Boom" in result.error


def test_executor_registers_interactive_command_tools(executor):
    """Verifica que ferramentas de comando interativo são registradas."""
    names = executor.registry.names()
    assert "run_shell_command" not in names
    assert "run_shell" in names
    assert "exec_command" in names
    assert "write_stdin" in names
    assert "close_command_session" in names
    assert "memory_save" in names
    assert "memory_retrieve" in names


@pytest.mark.parametrize("alias,arguments,check", [
    ("run", {"commands": ["echo hello"]}, "content"),
    ("execute_command", {"command": "echo hello"}, "status"),
])
def test_executor_normalizes_aliases(executor_with_workspace, alias, arguments, check):
    """Verifica que aliases de run são normalizados."""
    result = executor_with_workspace.execute(ToolCall(name=alias, arguments=arguments))
    assert result.ok is True
    if check == "content":
        assert "hello" in result.content
    else:
        assert result.data["status"] in {"running", "completed"}


# ════════════════════════════════════════════════════════════════════════
# Memory tool tests
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def memory_executor(tmp_path):
    """Executor configurado com memory_file para testes de memória."""
    return ToolExecutor(
        ToolRuntimeConfig(
            workspace=Workspace(tmp_path),
        ),
        MagicMock(),
    )


def test_executor_memory_save_and_retrieve_roundtrip(memory_executor):
    """Testa salvamento e recuperação de memória."""
    save = memory_executor.execute(
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
    retrieve = memory_executor.execute(
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


def test_executor_memory_retrieve_filters_by_prefix_and_tags(memory_executor):
    """Testa filtros de prefixo e tags na recuperação."""
    memory_executor.execute(
        ToolCall(
            name="memory_save",
            arguments={
                "namespace": "workspace",
                "key": "decision.api",
                "value": {"text": "v1", "tags": ["decision", "api"]},
            },
        )
    )
    memory_executor.execute(
        ToolCall(
            name="memory_save",
            arguments={
                "namespace": "workspace",
                "key": "decision.ui",
                "value": {"text": "v2", "tags": ["decision", "ui"]},
            },
        )
    )

    retrieve = memory_executor.execute(
        ToolCall(
            name="memory_retrieve",
            arguments={"namespace": "workspace", "prefix": "decision.", "tags": ["api"]},
        )
    )

    assert retrieve.ok is True
    assert [entry["key"] for entry in retrieve.data["entries"]] == ["decision.api"]


def test_executor_memory_list_namespaces_and_delete_roundtrip(tmp_path):
    """Testa listagem de namespaces e exclusão."""
    approval = MagicMock()
    approval.approve.return_value = True
    executor = ToolExecutor(
        ToolRuntimeConfig(
            workspace=Workspace(tmp_path),
        ),
        approval,
    )
    executor.execute(
        ToolCall(
            name="memory_save",
            arguments={"namespace": "workspace", "key": "summary", "value": "hello"},
        )
    )

    listed = executor.execute(ToolCall(name="memory_list_namespaces", arguments={}))
    deleted = executor.execute(
        ToolCall(name="memory_delete", arguments={"namespace": "workspace", "key": "summary"})
    )
    retrieved = executor.execute(
        ToolCall(name="memory_retrieve", arguments={"namespace": "workspace"})
    )

    assert listed.ok is True
    assert listed.data["namespaces"] == [{"namespace": "workspace", "keys": 1}]
    assert deleted.ok is True
    assert deleted.data["removed"] == 1
    assert retrieved.ok is True
    assert retrieved.data["entries"] == []
    approval.approve.assert_called_once()


def test_task_executor_skips_review_claim_when_agent_is_not_operational(tmp_path):
    """Verifica que TaskExecutor pula review quando agente não operacional."""
    repository = MagicMock()
    repository.claim_task.return_value = None
    executor = TaskExecutor("gemini", db_path=tmp_path / "tasks.db", poll_interval=0.01, repository=repository)
    executor.set_review_handler(lambda _task: True)
    executor.set_review_eligibility(lambda: False)
    executor._running = True

    def stop_loop(*_args, **_kwargs):
        executor._running = False
        return True

    with patch.object(executor, "_wait_or_stop", side_effect=stop_loop):
        executor._poll_loop()

    repository.claim_review_task.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# remove_file executor tests
# ════════════════════════════════════════════════════════════════════════

def test_executor_remove_file_is_registered(executor):
    """remove_file está registrado no executor."""
    assert "remove_file" in executor.registry.names()


@pytest.mark.parametrize("approved", [False, True], ids=["negado", "aprovado"])
def test_executor_remove_file_approval(executor_with_approval, approved, tmp_path):
    """remove_file é negado quando aprovação falha e executa quando aprovada."""
    executor_with_approval.approval_handler.approve.return_value = approved
    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    result = executor_with_approval.execute(call)
    if approved:
        assert result.ok is True
        assert "removido" in result.content.lower()
        assert not (tmp_path / "x.txt").exists()
    else:
        assert result.ok is False
        assert "Execução negada" in result.error


def test_executor_remove_file_no_approval_config_skips_handler(executor_no_approval, tmp_path):
    """Com require_approval_for_mutations=False o handler não é consultado."""
    executor_no_approval.approval_handler.approve.return_value = False
    (tmp_path / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    result = executor_no_approval.execute(call)
    assert result.ok is True
    executor_no_approval.approval_handler.approve.assert_not_called()


def test_executor_allows_mcp_tool_with_propagated_task_scope(tmp_path):
    """Escopo de task propagado pelo MCP autoriza tool mutante sem aprovação."""
    approval = MagicMock()
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), approval)
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


def test_executor_remove_file_policy_blocks_missing_dry_run(executor_with_workspace):
    """Política bloqueia remove_file sem dry_run=False explícito."""
    (executor_with_workspace.config.workspace.cwd / "x.txt").write_text("x")
    call = ToolCall(name="remove_file", arguments={"path": "x.txt"})
    result = executor_with_workspace.execute(call)
    assert result.ok is False
    assert "dry_run=False" in result.error


# ════════════════════════════════════════════════════════════════════════
# Spinner/cancel callbacks tests
# ════════════════════════════════════════════════════════════════════════

def test_set_spinner_callbacks_injects_into_approval_manager():
    """set_spinner_callbacks injeta no ApprovalManager diretamente."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), input_fn=lambda _: "y")
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), handler)

    suspend = MagicMock()
    resume = MagicMock()
    executor.set_spinner_callbacks(suspend, resume)

    assert handler._console_handler._suspend_spinner_fn[threading.get_ident()] is suspend
    assert handler._console_handler._resume_spinner_fn[threading.get_ident()] is resume


def test_set_spinner_callbacks_ignores_non_console_handler():
    """set_spinner_callbacks não quebra com handler sem _console_handler."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), input_fn=lambda _: "y")
    handler.set_approve_all(True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), handler)

    suspend = MagicMock()
    resume = MagicMock()
    # Não deve lançar exceção
    executor.set_spinner_callbacks(suspend, resume)


def test_set_approval_cancel_event_injects_into_approval_manager():
    """set_approval_cancel_event injeta cancel_event no ApprovalManager."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), input_fn=lambda _: "y")
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), handler)
    cancel_event = threading.Event()

    executor.set_approval_cancel_event(cancel_event)

    assert handler._console_handler._cancel_event is cancel_event


def test_bind_approval_cancel_event_is_thread_isolated_with_restore():
    """Binding de cancel_event é por thread e restaura o valor anterior."""
    executor = ToolExecutor(
        ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))),
        ApprovalManager(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), input_fn=lambda _: "y"),
    )
    event_a = threading.Event()
    event_b = threading.Event()
    results = {}
    errors = {}

    def driver(name, event):
        try:
            previous = executor.bind_approval_cancel_event(event)
            try:
                results[name] = executor.get_thread_approval_cancel_event()
            finally:
                executor.bind_approval_cancel_event(previous)
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc

    threads = [
        threading.Thread(target=driver, args=("a", event_a), daemon=True),
        threading.Thread(target=driver, args=("b", event_b), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)

    assert errors == {}, f"erros: {errors}"
    assert results == {"a": event_a, "b": event_b}
    assert executor.get_thread_approval_cancel_event() is None


def test_shared_approval_manager_cancel_event_is_thread_scoped_during_prompt():
    """Cancelar a thread B não cancela o prompt ativo da thread A."""
    started = threading.Event()
    can_proceed = threading.Event()

    def blocking_input(prompt):
        started.set()
        can_proceed.wait(5)
        return "y"

    manager = ApprovalManager(None, input_fn=blocking_input)
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), manager)
    event_a = threading.Event()
    event_b = threading.Event()
    results = {}
    errors = {}

    def driver(name, event):
        try:
            previous = executor.bind_approval_cancel_event(event)
            try:
                results[name] = manager.approve(tool_name="shell", summary="ls")
            finally:
                executor.bind_approval_cancel_event(previous)
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc

    t_a = threading.Thread(target=driver, args=("a", event_a), daemon=True)
    t_a.start()
    assert started.wait(3), "thread A deve entrar no prompt bloqueante"

    t_b = threading.Thread(target=driver, args=("b", event_b), daemon=True)
    t_b.start()

    event_b.set()
    time.sleep(0.2)
    can_proceed.set()

    t_a.join(3)
    t_b.join(3)

    assert errors == {}, f"erros: {errors}"
    assert results.get("a") is True
    assert results.get("b") is False


# ════════════════════════════════════════════════════════════════════════
# Unified approval flow tests
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("approved", [False, True])
def test_executor_permission_error_approval(config, approval_handler, approved):
    """Testa fluxo de permission_error com aprovação."""
    executor = ToolExecutor(config, approval_handler)
    approval_handler.approve.return_value = approved

    permission_error = PathPermissionError("/etc/passwd", Path("/etc/passwd"))

    call = ToolCall(name="list_files", arguments={"path": "."})
    with patch.object(executor.policy, "validate"), \
         patch.object(executor.policy, "check_path_permission", return_value=permission_error):
        result = executor.execute(call)

    if approved:
        assert result.ok is True
    else:
        assert result.ok is False
        assert "Execução negada" in result.error
        approval_handler.approve.assert_called_once()
        call_kwargs = approval_handler.approve.call_args.kwargs
        assert "Permissão necessária" in call_kwargs["summary"]


def test_executor_needs_approval_and_permission_error_unified(tmp_path):
    """Quando ferramenta tem needs_approval e permission_error, approve chamado uma vez."""
    config = ToolRuntimeConfig(
        workspace=Workspace(tmp_path),
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


# ════════════════════════════════════════════════════════════════════════
# write_stdin approval tests
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("require_approval,expected_approve_called", [
    (True, True),
    (False, False),
])
def test_executor_write_stdin_approval(require_approval, expected_approve_called):
    """write_stdin requer aprovação conforme configuração."""
    config = ToolRuntimeConfig(
        workspace=Workspace(Path("/tmp")),
        require_approval_for_mutations=require_approval,
    )
    approval_handler = MagicMock()
    approval_handler.approve.return_value = False
    executor = ToolExecutor(config, approval_handler)

    call = ToolCall(name="write_stdin", arguments={"session_id": 1, "chars": "y"})
    result = executor.execute(call)

    if expected_approve_called:
        assert result.ok is False
        assert "Execução negada" in result.error
        approval_handler.approve.assert_called_once()
    else:
        approval_handler.approve.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# Property tests
# ════════════════════════════════════════════════════════════════════════

def test_executor_approval_handler_property():
    """A property approval_handler retorna o handler configurado."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), input_fn=lambda _: "y")
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), handler)
    assert executor.approval_handler is handler


def test_set_spinner_callbacks_no_op_when_handler_is_none_like():
    """set_spinner_callbacks não quebra com handler sem atributo _base."""
    handler = ApprovalManager(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), input_fn=lambda _: "y")
    handler.set_approve_all(True)
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(Path("/tmp"))), handler)

    executor.set_spinner_callbacks(MagicMock(), MagicMock())
    # Não deve lançar exceção


# ════════════════════════════════════════════════════════════════════════
# Delegate via ToolExecutor (contrato de dispatch, pool ativo, truncamento)
# ════════════════════════════════════════════════════════════════════════

def test_executor_delegate_dispatches_with_delegation_mode(tmp_path):
    """delegate delega com contrato alinhado ao fluxo de delegation interno."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
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
    assert result.content.startswith("delegated ok")
    assert "delegação registrada como task" in result.content
    dispatch.assert_called_once()
    args, kwargs = dispatch.call_args
    assert args == ("codex",)
    assert kwargs["delegation"]["task"] == "ajuste de bug"
    assert kwargs["delegation"]["context"] == "arquivo quimera/runtime/executor.py"
    assert kwargs["delegation"]["delegation_id"].startswith("dlg-")
    assert kwargs["delegation"]["chain"] == ["codex"]
    assert kwargs == {
        "delegation": kwargs["delegation"],
        "delegation_only": True,
        "protocol_mode": "delegation",
        "primary": False,
        "silent": False,
        "show_output": False,
        "persist_history": True,
        "history_snapshot": [],
        "max_retries": 3,
        "from_agent": None,
        "progress_callback": None,
    }


def test_executor_delegate_fails_when_dispatch_not_injected(tmp_path):
    """delegate retorna erro explícito quando não há callback de dispatch."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())

    result = executor.execute(
        ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})
    )

    assert result.ok is False
    assert "not available" in (result.error or "")


def test_executor_delegate_internal_would_not_require_human_approval(tmp_path):
    """delegate interno passa por policy/broker, mas é auto-aprovado dentro do budget."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
    call = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})

    assert executor.would_require_approval(call) is False


def test_policy_phase_methods_for_delegate(tmp_path):
    """delegate agora passa por policy/approval broker como risco de delegação."""
    policy = ToolPolicy(ToolRuntimeConfig(workspace=Workspace(tmp_path)))
    delegate = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})
    read_file = ToolCall(name="read_file", arguments={"path": "x.txt"})

    assert policy.requires_validation(delegate) is True
    assert policy.requires_path_permission(delegate) is False
    assert policy.requires_approval(delegate) is True

    assert policy.requires_validation(read_file) is True
    assert policy.requires_path_permission(read_file) is True


def test_executor_delegate_goes_through_broker_without_human_prompt_when_internal(tmp_path):
    """delegate não bypassa policy, mas delegação interna é auto-aprovada no broker."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={"target_agent": "codex", "request": "x", "context": "ctx"},
        )
    )

    assert result.ok is True
    assert result.content.startswith("ok")
    assert "delegação registrada como task" in result.content
    executor.approval_handler.approve.assert_not_called()
    assert executor.approval_broker.audit_log[-1]["event"] == "auto_approved"
    dispatch.assert_called_once()


def test_executor_delegate_rejects_non_string_context(tmp_path):
    """delegate valida `context` localmente mesmo com bypass de policy."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
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
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())

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
    assert result.content.startswith("ok from claude")
    assert "delegação registrada como task" in result.content
    assert [c.args[0] for c in spy.call_args_list] == ["codex", "claude"]


def test_executor_delegate_supports_multiple_sequential_delegations(tmp_path):
    """delegate suporta delegations múltiplos em sequência no mesmo payload."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())

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


def test_executor_delegate_propagates_role_and_access_list(tmp_path):
    """delegate propaga role/access_list para step principal e steps adicionais."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
    dispatch = MagicMock(return_value="ok")
    executor.set_delegate_fn(dispatch)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "implemente",
                "role": "executor",
                "access_list": ["diff", "tests"],
                "steps": [
                    {
                        "target_agent": "claude",
                        "request": "revise",
                        "role": "reviewer",
                        "access_list": ["diff"],
                    }
                ],
            },
        )
    )

    assert result.ok is True
    delegations = [call.kwargs["delegation"] for call in dispatch.call_args_list]
    assert delegations[0]["role"] == "executor"
    assert delegations[0]["access_list"] == ["diff", "tests"]
    assert delegations[1]["role"] == "reviewer"
    assert delegations[1]["access_list"] == ["diff"]


def test_executor_delegate_fallback_inherits_role_and_access_list(tmp_path):
    """fallback_agents herdam role/access_list do step correspondente."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())

    def dispatch(agent_name, **kwargs):
        if agent_name == "codex":
            return None
        delegation = kwargs["delegation"]
        return f"{agent_name}:{delegation.get('role')}:{','.join(delegation.get('access_list', []))}"

    spy = MagicMock(side_effect=dispatch)
    executor.set_delegate_fn(spy)

    result = executor.execute(
        ToolCall(
            name="delegate",
            arguments={
                "target_agent": "codex",
                "request": "implemente",
                "role": "executor",
                "access_list": ["diff", "tests"],
                "fallback_agents": ["claude"],
            },
        )
    )

    assert result.ok is True
    assert result.content.startswith("claude:executor:diff,tests")
    assert "delegação registrada como task" in result.content


def test_executor_delegate_rejects_agents_outside_active_pool(tmp_path):
    """delegate deve rejeitar alvos que não estão no pool ativo da sessão."""
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
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
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())

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
    executor = ToolExecutor(ToolRuntimeConfig(workspace=Workspace(tmp_path)), MagicMock())
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
