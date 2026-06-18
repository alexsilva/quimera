from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy, ToolPolicyError, is_path_inside
from quimera.runtime.registry import ToolRegistry
from quimera.runtime.tools import delegate as delegate_module
from quimera.runtime.tools import files as files_tools
from quimera.runtime.tools import memory as memory_tools
from quimera.runtime.tools import patch as patch_tools
from quimera.runtime.tools import tasks as tasks_tools
from quimera.runtime.tools import todo as todo_tools
from quimera.runtime.tools.shell import ShellToolValidator
from quimera.runtime.tools.web import WebToolValidator


def _make_policy(config):
    """Cria ToolPolicy com todos os validators registrados."""
    p = ToolPolicy(config)
    _reg = ToolRegistry()
    files_tools.register(_reg, p, config)
    patch_tools.register(_reg, p, config)
    tasks_tools.register(_reg, p, config)
    todo_tools.register(_reg, p, config)
    memory_tools.register(_reg, p, config)
    delegate_module.register(_reg, p, config)
    return p


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


@pytest.fixture
def policy(config):
    return _make_policy(config)


@pytest.fixture
def shell_validator(config):
    return ShellToolValidator(config)


def test_policy_validate_unknown_tool(policy):
    """Verifica que policy validate unknown tool."""
    call = ToolCall(name="unknown", arguments={})
    with pytest.raises(ToolPolicyError, match="Sem política"):
        policy.validate(call)


def test_policy_read_file_no_path(policy):
    """Verifica que policy read file no path."""
    call = ToolCall(name="read_file", arguments={})
    with pytest.raises(ToolPolicyError, match="requer 'path'"):
        policy.validate(call)


def test_policy_write_file_no_content(policy):
    """Verifica que policy write file no content."""
    call = ToolCall(name="write_file", arguments={"path": "test.txt"})
    with pytest.raises(ToolPolicyError, match="requer 'content'"):
        policy.validate(call)


def test_policy_write_file_existing_requires_replace_flag(tmp_path):
    """Verifica que policy write file existing requires replace flag."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)
    (tmp_path / "test.txt").write_text("old", encoding="utf-8")
    call = ToolCall(name="write_file", arguments={"path": "test.txt", "content": "new"})
    with pytest.raises(ToolPolicyError, match="replace_existing=true"):
        policy.validate(call)


def test_policy_write_file_existing_allowed_with_replace_flag(tmp_path):
    """Verifica que policy write file existing allowed with replace flag."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)
    (tmp_path / "test.txt").write_text("old", encoding="utf-8")
    call = ToolCall(
        name="write_file",
        arguments={"path": "test.txt", "content": "new", "replace_existing": True},
    )
    policy.validate(call)


def test_policy_apply_patch_requires_patch(policy):
    """Verifica que policy apply patch requires patch."""
    call = ToolCall(name="apply_patch", arguments={})
    with pytest.raises(ToolPolicyError, match="apply_patch requer 'patch'"):
        policy.validate(call)


def test_policy_grep_search_no_pattern(policy):
    """Verifica que policy grep search no pattern."""
    # Line 47 coverage
    call = ToolCall(name="grep_search", arguments={"pattern": ""})
    with pytest.raises(ToolPolicyError, match="requer um padrão não vazio"):
        policy.validate(call)


def test_policy_propose_task_disabled(policy):
    """Verifica que policy propose task disabled."""
    # Line 53 coverage
    call = ToolCall(name="propose_task", arguments={})
    with pytest.raises(ToolPolicyError, match="foi desativada"):
        policy.validate(call)


def test_policy_shell_empty(shell_validator):
    """Verifica que policy shell empty."""
    call = ToolCall(name="run_shell", arguments={"command": "  "})
    with pytest.raises(ToolPolicyError, match="requer um comando não vazio"):
        shell_validator.validate(call)


def test_policy_run_shell_command_alias(policy):
    """Verifica que policy run shell command alias."""
    call = ToolCall(name="run_shell_command", arguments={"command": "ls"})
    policy.validate(call)


def test_policy_exec_command_empty(shell_validator):
    """Verifica que policy exec command empty."""
    call = ToolCall(name="exec_command", arguments={"cmd": "  "})
    with pytest.raises(ToolPolicyError, match="exec_command requer um comando não vazio"):
        shell_validator.validate(call)


def test_policy_write_stdin_requires_session_id(shell_validator):
    """Verifica que policy write stdin requires session id."""
    call = ToolCall(name="write_stdin", arguments={})
    with pytest.raises(ToolPolicyError, match="session_id"):
        shell_validator.validate(call)


def test_policy_write_stdin_requires_integer_session_id(shell_validator):
    """Verifica que policy write stdin requires integer session id."""
    call = ToolCall(name="write_stdin", arguments={"session_id": "abc"})
    with pytest.raises(ToolPolicyError, match="session_id inteiro"):
        shell_validator.validate(call)


def test_policy_close_command_session_requires_session_id(shell_validator):
    """Verifica que policy close command session requires session id."""
    call = ToolCall(name="close_command_session", arguments={})
    with pytest.raises(ToolPolicyError, match="session_id"):
        shell_validator.validate(call)


def test_policy_shell_denylist(shell_validator):
    """Verifica que policy shell denylist."""
    call = ToolCall(name="run_shell", arguments={"command": "rm -rf /"})
    with pytest.raises(ToolPolicyError, match="Comando bloqueado pela denylist"):
        shell_validator.validate(call)


def test_policy_shell_chain_operator(shell_validator):
    """Verifica que policy shell chain operator."""
    call = ToolCall(name="run_shell", arguments={"command": "ls && cat"})
    with pytest.raises(ToolPolicyError, match="operador de encadeamento proibido"):
        shell_validator.validate(call)


def test_policy_shell_invalid_shlex(shell_validator):
    """Verifica que policy shell invalid shlex."""
    call = ToolCall(name="run_shell", arguments={"command": 'echo "unclosed quote'})
    with pytest.raises(ToolPolicyError, match="Comando inválido"):
        shell_validator.validate(call)


def test_policy_shell_not_in_allowlist(shell_validator):
    """Verifica que policy shell not in allowlist."""
    call = ToolCall(name="run_shell", arguments={"command": "nc -l 8080"})
    with pytest.raises(ToolPolicyError, match="fora da allowlist"):
        shell_validator.validate(call)


def test_policy_path_outside_workspace(policy):
    """Verifica que policy path outside workspace."""
    call = ToolCall(name="read_file", arguments={"path": "../../etc/passwd"})
    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        policy.validate(call)


def test_policy_path_prefix_sibling_outside_workspace(tmp_path):
    """Evita bypass por prefixo de path (workspace vs workspace2)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace2"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("TOPSECRET", encoding="utf-8")

    policy = _make_policy(ToolRuntimeConfig(workspace_root=workspace))
    call = ToolCall(name="read_file", arguments={"path": "../workspace2/secret.txt"})
    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        policy.validate(call)


def test_policy_requires_approval(policy):
    """Verifica que policy requires approval."""
    assert policy.requires_approval(ToolCall(name="write_file", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="apply_patch", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="run_shell_command", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="read_file", arguments={})) is False


def test_policy_other_validations(policy):
    """Verifica que policy other validations."""
    # Just to hit the pass/return lines
    policy.validate(ToolCall(name="list_tasks", arguments={"job_id": 1}))
    policy.validate(ToolCall(name="list_jobs", arguments={}))
    policy.validate(ToolCall(name="get_job", arguments={}))
    policy.validate(ToolCall(name="memory_retrieve", arguments={}))
    policy.validate(ToolCall(name="todo_list", arguments={}))
    policy.validate(
        ToolCall(name="todo_write", arguments={"todos": [{"content": "task"}]})
    )


def test_policy_todo_write_empty_todos(policy):
    """Verifica que policy todo write empty todos."""
    call = ToolCall(name="todo_write", arguments={"todos": []})
    with pytest.raises(ToolPolicyError, match="lista não vazia"):
        policy.validate(call)


def test_policy_todo_write_missing_todos(policy):
    """Verifica que policy todo write missing todos."""
    call = ToolCall(name="todo_write", arguments={})
    with pytest.raises(ToolPolicyError, match="lista não vazia"):
        policy.validate(call)


def test_policy_todo_write_invalid_item_type(policy):
    """Verifica que policy todo write invalid item type."""
    call = ToolCall(name="todo_write", arguments={"todos": ["not a dict"]})
    with pytest.raises(ToolPolicyError, match="deve ser um dicionário"):
        policy.validate(call)


def test_policy_todo_write_missing_content(policy):
    """Verifica que policy todo write missing content."""
    call = ToolCall(name="todo_write", arguments={"todos": [{"priority": "high"}]})
    with pytest.raises(ToolPolicyError, match="requer 'content' não vazio"):
        policy.validate(call)


def test_policy_todo_write_invalid_status(policy):
    """Verifica que policy todo write invalid status."""
    call = ToolCall(name="todo_write", arguments={"todos": [{"content": "x", "status": "invalid"}]})
    with pytest.raises(ToolPolicyError, match="status inválido"):
        policy.validate(call)


def test_policy_todo_write_invalid_priority(policy):
    """Verifica que policy todo write invalid priority."""
    call = ToolCall(name="todo_write", arguments={"todos": [{"content": "x", "priority": "urgent"}]})
    with pytest.raises(ToolPolicyError, match="priority inválida"):
        policy.validate(call)


def test_policy_todo_write_valid(policy):
    """Verifica que policy todo write valid."""
    call = ToolCall(name="todo_write", arguments={"todos": [{"content": "task"}]})
    policy.validate(call)


def test_policy_web_fetch_accepts_url(policy, config):
    """Verifica que policy web fetch accepts url."""
    policy.register_tool_validator(["web_fetch"], WebToolValidator(config))
    call = ToolCall(name="web_fetch", arguments={"url": "https://example.com"})
    policy.validate(call)


def test_policy_web_fetch_accepts_url_string(policy, config):
    """Verifica que policy web fetch accepts url string."""
    policy.register_tool_validator(["web_fetch"], WebToolValidator(config))
    call = ToolCall(name="web_fetch", arguments={"url": "https://example.com"})
    policy.validate(call)


def test_policy_web_fetch_rejects_empty_url(policy, config):
    """Verifica que policy web fetch rejects empty url."""
    policy.register_tool_validator(["web_fetch"], WebToolValidator(config))
    call = ToolCall(name="web_fetch", arguments={"url": " "})
    with pytest.raises(ToolPolicyError, match="url' não vazia"):
        policy.validate(call)


def test_policy_disabled_tool_exceptions(policy):
    """Verifica que policy disabled tool exceptions."""
    for tool in ["approve_task", "complete_task", "fail_task"]:
        with pytest.raises(ToolPolicyError):
            policy.validate(ToolCall(name=tool, arguments={}))


# ── remove_file policy ───────────────────────────────────────

def test_policy_remove_file_requires_path(policy):
    """remove_file sem 'path' é rejeitado."""
    call = ToolCall(name="remove_file", arguments={})
    with pytest.raises(ToolPolicyError, match="requer 'path'"):
        policy.validate(call)


def test_policy_remove_file_requires_explicit_dry_run_false(tmp_path):
    """dry_run deve ser explicitamente False; True (padrão) é rejeitado."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt"})
    with pytest.raises(ToolPolicyError, match="dry_run=False explícito"):
        policy.validate(call)


def test_policy_remove_file_allows_explicit_false(tmp_path):
    """dry_run=False explícito passa na validação."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    policy.validate(call)  # não deve lançar


def test_policy_remove_file_rejects_dry_run_true(tmp_path):
    """dry_run=True também é rejeitado (só False passa)."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": True})
    with pytest.raises(ToolPolicyError, match="dry_run=False explícito"):
        policy.validate(call)


def test_policy_remove_file_outside_workspace(tmp_path):
    """remove_file com path fora do workspace é rejeitado."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)

    call = ToolCall(name="remove_file", arguments={"path": "../../etc/passwd", "dry_run": False})
    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        policy.validate(call)


def test_policy_remove_file_requires_approval(policy):
    """remove_file requer aprovação quando require_approval_for_mutations=True."""
    assert policy.requires_approval(ToolCall(name="remove_file", arguments={})) is True


def test_policy_check_path_permission_for_remove_file(policy):
    """check_path_permission cobre remove_file."""
    call = ToolCall(name="remove_file", arguments={"path": ".", "dry_run": False})
    result = policy.check_path_permission(call)
    # path '.' resolve para /tmp (workspace_root), que está nos allowed_read_roots
    assert result is None


def test_policy_check_path_permission_outside_for_remove_file(policy):
    """check_path_permission retorna erro para path fora dos roots."""
    call = ToolCall(name="remove_file", arguments={"path": "../../etc/passwd", "dry_run": False})
    result = policy.check_path_permission(call)
    assert result is not None
    assert "etc" in str(result.resolved_path)


# ── requires_approval expansão ─────────────────────────────

def test_policy_requires_approval_for_all_mutational_tools(policy):
    """Todas as ferramentas mutacionais requerem aprovação."""
    mutational = [
        "write_file",
        "apply_patch",
        "run_shell",
        "run_shell_command",
        "exec_command",
        "close_command_session",
        "remove_file",
        "write_stdin",
    ]
    for tool_name in mutational:
        assert policy.requires_approval(ToolCall(name=tool_name, arguments={})) is True, \
            f"{tool_name} deveria requerer aprovação"


def test_policy_does_not_require_approval_for_read_tools(policy):
    """Ferramentas de leitura não requerem aprovação."""
    readonly = ["read_file", "list_files", "grep_search", "list_tasks", "list_jobs", "get_job", "memory_save", "memory_retrieve", "todo_list"]
    for tool_name in readonly:
        assert policy.requires_approval(ToolCall(name=tool_name, arguments={})) is False, \
            f"{tool_name} NÃO deveria requerer aprovação"


def test_policy_check_path_permission_none_for_non_path_tools(policy):
    """check_path_permission retorna None para ferramentas que não operam em paths."""
    non_path_tools = ["run_shell", "write_file", "apply_patch", "write_stdin"]
    for tool_name in non_path_tools:
        call = ToolCall(name=tool_name, arguments={})
        assert policy.check_path_permission(call) is None


# ── write_stdin policy ──────────────────────────────────────

def test_policy_write_stdin_requires_yield_time_ms_integer(shell_validator):
    """write_stdin com yield_time_ms não inteiro é rejeitado."""
    call = ToolCall(name="write_stdin", arguments={
        "session_id": 1,
        "yield_time_ms": "abc",
    })
    with pytest.raises(ToolPolicyError, match="yield_time_ms inteiro"):
        shell_validator.validate(call)


def test_policy_memory_save_valid(policy):
    policy.validate(
        ToolCall(
            name="memory_save",
            arguments={"namespace": "workspace", "key": "summary", "value": {"text": "ok"}, "ttl_seconds": 60},
        )
    )


def test_policy_memory_save_rejects_path_like_key(policy):
    with pytest.raises(ToolPolicyError, match="key não pode conter path"):
        policy.validate(
            ToolCall(
                name="memory_save",
                arguments={"namespace": "workspace", "key": "../secret", "value": {"text": "ok"}},
            )
        )


def test_policy_memory_retrieve_rejects_invalid_tags(policy):
    with pytest.raises(ToolPolicyError, match="tags deve conter apenas strings não vazias"):
        policy.validate(
            ToolCall(
                name="memory_retrieve",
                arguments={"tags": ["ok", ""]},
            )
        )


def test_policy_write_stdin_valid(shell_validator):
    """write_stdin com session_id inteiro válido passa."""
    call = ToolCall(name="write_stdin", arguments={"session_id": 1})
    shell_validator.validate(call)  # não deve lançar


def test_policy_write_stdin_valid_with_yield_time_ms(shell_validator):
    """write_stdin com yield_time_ms inteiro passa."""
    call = ToolCall(name="write_stdin", arguments={
        "session_id": 1,
        "yield_time_ms": 100,
    })
    shell_validator.validate(call)


# ── close_command_session policy ────────────────────────────

def test_policy_close_command_session_requires_integer_session_id(shell_validator):
    """close_command_session com session_id não inteiro é rejeitado."""
    call = ToolCall(name="close_command_session", arguments={"session_id": "abc"})
    with pytest.raises(ToolPolicyError, match="session_id inteiro"):
        shell_validator.validate(call)


def test_policy_close_command_session_valid(shell_validator):
    """close_command_session com session_id inteiro passa."""
    call = ToolCall(name="close_command_session", arguments={"session_id": 1})
    shell_validator.validate(call)


# ── exec_command policy ─────────────────────────────────────

def test_policy_exec_command_with_workdir(shell_validator):
    """exec_command com workdir dentro do workspace passa."""
    call = ToolCall(name="exec_command", arguments={
        "cmd": "echo hello",
        "workdir": ".",
    })
    shell_validator.validate(call)


def test_policy_exec_command_with_workdir_outside_workspace(shell_validator):
    """exec_command com workdir fora do workspace é rejeitado."""
    call = ToolCall(name="exec_command", arguments={
        "cmd": "echo hello",
        "workdir": "../../etc",
    })
    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        shell_validator.validate(call)


# ── shell policy ────────────────────────────────────────────

def test_policy_shell_chain_operators_all(shell_validator):
    """Todos os operadores de encadeamento são bloqueados."""
    for op in [";", "&&", "||", "|", "`", "$("]:
        call = ToolCall(name="run_shell", arguments={"command": f"ls {op} cat"})
        with pytest.raises(ToolPolicyError, match="operador de encadeamento proibido"):
            shell_validator.validate(call)


def test_policy_shell_allowlist_validation(shell_validator):
    """Comandos na allowlist passam na validação."""
    call = ToolCall(name="run_shell", arguments={"command": "ls -la"})
    shell_validator.validate(call)  # não deve lançar


# ── blocked_tools ───────────────────────────────────────────

# ── delegate policy ─────────────────────────────────────────

def test_policy_delegate_valid(policy):
    """delegate com campos mínimos válidos passa."""
    call = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "faça algo"})
    policy.validate(call)


def test_policy_delegate_missing_target_agent(policy):
    """delegate sem target_agent é rejeitado."""
    call = ToolCall(name="delegate", arguments={"request": "faça algo"})
    with pytest.raises(ToolPolicyError, match="target_agent"):
        policy.validate(call)


def test_policy_delegate_missing_request(policy):
    """delegate sem request é rejeitado."""
    call = ToolCall(name="delegate", arguments={"target_agent": "codex"})
    with pytest.raises(ToolPolicyError, match="request"):
        policy.validate(call)


def test_policy_delegate_reserved_field(policy):
    """delegate com campo reservado é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex", "request": "faça algo", "run_id": "abc"
    })
    with pytest.raises(ToolPolicyError, match="campos reservados"):
        policy.validate(call)


def test_policy_delegate_steps_not_a_list(policy):
    """delegate.steps não-lista é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex", "request": "faça algo", "steps": "não é lista"
    })
    with pytest.raises(ToolPolicyError, match="steps deve ser uma lista"):
        policy.validate(call)


def test_policy_delegate_steps_item_not_dict(policy):
    """delegate.steps com item não-dict é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex", "request": "faça algo", "steps": ["string"]
    })
    with pytest.raises(ToolPolicyError, match=r"steps\[0\] deve ser um objeto"):
        policy.validate(call)


def test_policy_delegate_steps_item_missing_target_agent(policy):
    """delegate.steps com target_agent ausente é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [{"request": "próximo passo"}],
    })
    with pytest.raises(ToolPolicyError, match=r"steps\[0\].target_agent"):
        policy.validate(call)


def test_policy_delegate_steps_item_missing_request(policy):
    """delegate.steps com request ausente é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [{"target_agent": "opencode"}],
    })
    with pytest.raises(ToolPolicyError, match=r"steps\[0\].request"):
        policy.validate(call)


def test_policy_delegate_steps_valid(policy):
    """delegate.steps com itens válidos passa."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [{"target_agent": "opencode", "request": "próximo passo"}],
    })
    policy.validate(call)


def test_policy_delegate_blocked_tools(policy):
    """delegate é bloqueado quando na lista blocked_tools."""
    policy.blocked_tools = ["delegate"]
    call = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})
    with pytest.raises(ToolPolicyError, match="bloqueada pelo modo de execução ativo"):
        policy.validate(call)


def test_policy_delegate_requires_approval(policy):
    """delegate requer aprovação."""
    assert policy.requires_approval(ToolCall(name="delegate", arguments={})) is True


def test_policy_delegate_steps_empty_target_agent(policy):
    """delegate.steps com target_agent em branco é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [{"target_agent": "  ", "request": "algo"}],
    })
    with pytest.raises(ToolPolicyError, match=r"steps\[0\].target_agent"):
        policy.validate(call)


def test_policy_delegate_steps_empty_request(policy):
    """delegate.steps com request em branco é rejeitado."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [{"target_agent": "opencode", "request": ""}],
    })
    with pytest.raises(ToolPolicyError, match=r"steps\[0\].request"):
        policy.validate(call)


def test_policy_delegate_steps_second_item_invalid(policy):
    """delegate.steps valida todos os itens, não só o primeiro."""
    call = ToolCall(name="delegate", arguments={
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [
            {"target_agent": "opencode", "request": "passo 1"},
            {"target_agent": "opencode"},  # falta request
        ],
    })
    with pytest.raises(ToolPolicyError, match=r"steps\[1\].request"):
        policy.validate(call)


def test_policy_blocked_tools(policy):
    """Ferramentas na lista blocked_tools são rejeitadas."""
    policy.blocked_tools = ["list_files"]
    call = ToolCall(name="list_files", arguments={})
    with pytest.raises(ToolPolicyError, match="bloqueada pelo modo de execução ativo"):
        policy.validate(call)


# ── _resolve_workspace_path ─────────────────────────────────

def test_policy_resolve_workspace_path_empty_becomes_current(tmp_path):
    """Path vazio resolve para '.' (diretório corrente)."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = _make_policy(config)
    # _resolve_workspace_path é chamada via validate
    call = ToolCall(name="list_files", arguments={"path": ""})
    policy.validate(call)  # path vazio → "." → resolve para workspace_root


# ── PathPermissionError ─────────────────────────────────────

def test_path_permission_error_attributes():
    """PathPermissionError guarda raw_path e resolved_path."""
    from quimera.runtime.policy import PathPermissionError
    raw = "/etc/shadow"
    resolved = Path("/etc/shadow")
    exc = PathPermissionError(raw, resolved)
    assert exc.raw_path == raw
    assert exc.resolved_path == resolved
    assert "Permissão necessária" in str(exc)


# ── check_path_permission para list_files e grep_search ─────

def test_policy_check_path_permission_for_list_files(policy):
    """check_path_permission cobre list_files."""
    call = ToolCall(name="list_files", arguments={"path": "."})
    result = policy.check_path_permission(call)
    assert result is None


def test_policy_check_path_permission_for_grep_search(policy):
    """check_path_permission cobre grep_search."""
    call = ToolCall(name="grep_search", arguments={"pattern": "test"})
    result = policy.check_path_permission(call)
    assert result is None


def test_policy_check_path_permission_for_grep_search_outside(policy):
    """check_path_permission retorna erro para grep_search fora dos roots."""
    call = ToolCall(name="grep_search", arguments={
        "pattern": "test",
        "path": "../../etc/passwd",
    })
    result = policy.check_path_permission(call)
    assert result is not None


def test_policy_check_path_permission_for_list_files_outside(policy):
    """check_path_permission retorna erro para list_files fora dos roots."""
    call = ToolCall(name="list_files", arguments={"path": "../../etc"})
    result = policy.check_path_permission(call)
    assert result is not None


def test_policy_check_path_permission_rejects_prefix_sibling(tmp_path):
    """check_path_permission também bloqueia bypass por prefixo de path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace2"
    sibling.mkdir()

    policy = _make_policy(ToolRuntimeConfig(workspace_root=workspace))
    call = ToolCall(name="list_files", arguments={"path": "../workspace2"})
    result = policy.check_path_permission(call)
    assert result is not None


# ── shell file-path validation ──────────────────────────────

def test_policy_shell_file_cmd_absolute_outside_workspace(shell_validator):
    """cat com path absoluto fora do workspace é bloqueado."""
    call = ToolCall(name="run_shell", arguments={"command": "cat /etc/passwd"})
    with pytest.raises(ToolPolicyError, match="fora do workspace"):
        shell_validator.validate(call)


def test_policy_shell_file_cmd_tilde_outside_workspace(shell_validator):
    """cat com ~ expandindo para fora do workspace é bloqueado."""
    call = ToolCall(name="run_shell", arguments={"command": "cat ~/.ssh/id_rsa"})
    with pytest.raises(ToolPolicyError, match="fora do workspace"):
        shell_validator.validate(call)


def test_policy_shell_file_cmd_absolute_inside_workspace(tmp_path):
    """cat com path absoluto dentro do workspace é permitido."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    validator = ShellToolValidator(config)
    target = tmp_path / "file.txt"
    target.write_text("data")
    call = ToolCall(name="run_shell", arguments={"command": f"cat {target}"})
    validator.validate(call)


def test_policy_shell_file_cmd_relative_path_allowed(shell_validator):
    """cat com path relativo passa (resolve dentro do workdir do processo)."""
    call = ToolCall(name="run_shell", arguments={"command": "cat requirements.txt"})
    shell_validator.validate(call)


def test_policy_shell_non_file_cmd_absolute_path_ignored(shell_validator):
    """Comandos fora de _FILE_PATH_CMDS não têm validação de path."""
    call = ToolCall(name="run_shell", arguments={"command": "echo /etc/passwd"})
    shell_validator.validate(call)


def test_policy_shell_file_cmd_flag_not_validated(shell_validator):
    """Flags (tokens iniciados com -) não são tratadas como paths."""
    call = ToolCall(name="run_shell", arguments={"command": "ls -la"})
    shell_validator.validate(call)


# ── is_path_inside ─────────────────────────────────────────

def test_is_path_inside_same_dir(tmp_path):
    """Verifica que is path inside same dir."""
    assert is_path_inside(tmp_path / "file.txt", tmp_path) is True


def test_is_path_inside_subdir(tmp_path):
    """Verifica que is path inside subdir."""
    sub = tmp_path / "sub"
    sub.mkdir()
    assert is_path_inside(sub / "file.txt", tmp_path) is True


def test_is_path_inside_outside(tmp_path):
    """Verifica que is path inside outside."""
    assert is_path_inside(Path("/etc/passwd"), tmp_path) is False


def test_is_path_inside_prefix_sibling(tmp_path):
    """Verifica que is path inside prefix sibling."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace2"
    sibling.mkdir()
    assert is_path_inside(sibling / "secret.txt", workspace) is False


def test_is_path_inside_exact_root(tmp_path):
    """Verifica que is path inside exact root."""
    assert is_path_inside(tmp_path, tmp_path) is True


def test_is_path_inside_symlink(tmp_path):
    """Verifica que is path inside symlink."""
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "target.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert is_path_inside(link, tmp_path) is True


def test_is_path_inside_with_symlinked_root(tmp_path):
    """Verifica que is path inside with symlinked root."""
    real_root = tmp_path / "workspace"
    real_root.mkdir()
    root_alias = tmp_path / "workspace-link"
    root_alias.symlink_to(real_root, target_is_directory=True)

    child = real_root / "file.txt"
    assert is_path_inside(child, root_alias) is True
