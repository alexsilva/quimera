from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy, ToolPolicyError


@pytest.fixture
def config():
    return ToolRuntimeConfig(workspace_root=Path("/tmp"))


@pytest.fixture
def policy(config):
    return ToolPolicy(config)


def test_policy_validate_unknown_tool(policy):
    call = ToolCall(name="unknown", arguments={})
    with pytest.raises(ToolPolicyError, match="Sem política"):
        policy.validate(call)


def test_policy_read_file_no_path(policy):
    call = ToolCall(name="read_file", arguments={})
    with pytest.raises(ToolPolicyError, match="requer 'path'"):
        policy.validate(call)


def test_policy_write_file_no_content(policy):
    call = ToolCall(name="write_file", arguments={"path": "test.txt"})
    with pytest.raises(ToolPolicyError, match="requer 'content'"):
        policy.validate(call)


def test_policy_write_file_existing_requires_replace_flag(tmp_path):
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = ToolPolicy(config)
    (tmp_path / "test.txt").write_text("old", encoding="utf-8")
    call = ToolCall(name="write_file", arguments={"path": "test.txt", "content": "new"})
    with pytest.raises(ToolPolicyError, match="replace_existing=true"):
        policy.validate(call)


def test_policy_write_file_existing_allowed_with_replace_flag(tmp_path):
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = ToolPolicy(config)
    (tmp_path / "test.txt").write_text("old", encoding="utf-8")
    call = ToolCall(
        name="write_file",
        arguments={"path": "test.txt", "content": "new", "replace_existing": True},
    )
    policy.validate(call)


def test_policy_apply_patch_requires_patch(policy):
    call = ToolCall(name="apply_patch", arguments={})
    with pytest.raises(ToolPolicyError, match="apply_patch requer 'patch'"):
        policy.validate(call)


def test_policy_grep_search_no_pattern(policy):
    # Line 47 coverage
    call = ToolCall(name="grep_search", arguments={"pattern": ""})
    with pytest.raises(ToolPolicyError, match="requer um padrão não vazio"):
        policy.validate(call)


def test_policy_propose_task_disabled(policy):
    # Line 53 coverage
    call = ToolCall(name="propose_task", arguments={})
    with pytest.raises(ToolPolicyError, match="foi desativada"):
        policy.validate(call)


def test_policy_shell_empty(policy):
    # Line 74 coverage
    call = ToolCall(name="run_shell", arguments={"command": "  "})
    with pytest.raises(ToolPolicyError, match="requer um comando não vazio"):
        policy.validate(call)


def test_policy_run_shell_command_alias(policy):
    call = ToolCall(name="run_shell_command", arguments={"command": "ls"})
    policy.validate(call)


def test_policy_exec_command_empty(policy):
    call = ToolCall(name="exec_command", arguments={"cmd": "  "})
    with pytest.raises(ToolPolicyError, match="exec_command requer um comando não vazio"):
        policy.validate(call)


def test_policy_write_stdin_requires_session_id(policy):
    call = ToolCall(name="write_stdin", arguments={})
    with pytest.raises(ToolPolicyError, match="session_id"):
        policy.validate(call)


def test_policy_write_stdin_requires_integer_session_id(policy):
    call = ToolCall(name="write_stdin", arguments={"session_id": "abc"})
    with pytest.raises(ToolPolicyError, match="session_id inteiro"):
        policy.validate(call)


def test_policy_close_command_session_requires_session_id(policy):
    call = ToolCall(name="close_command_session", arguments={})
    with pytest.raises(ToolPolicyError, match="session_id"):
        policy.validate(call)


def test_policy_shell_denylist(policy):
    # Line 81 coverage
    call = ToolCall(name="run_shell", arguments={"command": "rm -rf /"})
    with pytest.raises(ToolPolicyError, match="Comando bloqueado pela denylist"):
        policy.validate(call)


def test_policy_shell_chain_operator(policy):
    call = ToolCall(name="run_shell", arguments={"command": "ls && cat"})
    with pytest.raises(ToolPolicyError, match="operador de encadeamento proibido"):
        policy.validate(call)


def test_policy_shell_invalid_shlex(policy):
    # Line 91-92 coverage
    call = ToolCall(name="run_shell", arguments={"command": 'echo "unclosed quote'})
    with pytest.raises(ToolPolicyError, match="Comando inválido"):
        policy.validate(call)


def test_policy_shell_not_in_allowlist(policy):
    call = ToolCall(name="run_shell", arguments={"command": "nc -l 8080"})
    with pytest.raises(ToolPolicyError, match="fora da allowlist"):
        policy.validate(call)


def test_policy_path_outside_workspace(policy):
    call = ToolCall(name="read_file", arguments={"path": "../../etc/passwd"})
    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        policy.validate(call)


def test_policy_requires_approval(policy):
    assert policy.requires_approval(ToolCall(name="write_file", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="apply_patch", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="run_shell_command", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="read_file", arguments={})) is False


def test_policy_other_validations(policy):
    # Just to hit the pass/return lines
    policy.validate(ToolCall(name="list_tasks", arguments={}))
    policy.validate(ToolCall(name="list_jobs", arguments={}))
    policy.validate(ToolCall(name="get_job", arguments={}))


def test_policy_disabled_tool_exceptions(policy):
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
    policy = ToolPolicy(config)
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt"})
    with pytest.raises(ToolPolicyError, match="dry_run=False explícito"):
        policy.validate(call)


def test_policy_remove_file_allows_explicit_false(tmp_path):
    """dry_run=False explícito passa na validação."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = ToolPolicy(config)
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": False})
    policy.validate(call)  # não deve lançar


def test_policy_remove_file_rejects_dry_run_true(tmp_path):
    """dry_run=True também é rejeitado (só False passa)."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = ToolPolicy(config)
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "x.txt", "dry_run": True})
    with pytest.raises(ToolPolicyError, match="dry_run=False explícito"):
        policy.validate(call)


def test_policy_remove_file_outside_workspace(tmp_path):
    """remove_file com path fora do workspace é rejeitado."""
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    policy = ToolPolicy(config)

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
    readonly = ["read_file", "list_files", "grep_search", "list_tasks", "list_jobs", "get_job"]
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

def test_policy_write_stdin_requires_yield_time_ms_integer(policy):
    """write_stdin com yield_time_ms não inteiro é rejeitado."""
    call = ToolCall(name="write_stdin", arguments={
        "session_id": 1,
        "yield_time_ms": "abc",
    })
    with pytest.raises(ToolPolicyError, match="yield_time_ms inteiro"):
        policy.validate(call)


def test_policy_write_stdin_valid(policy):
    """write_stdin com session_id inteiro válido passa."""
    call = ToolCall(name="write_stdin", arguments={"session_id": 1})
    policy.validate(call)  # não deve lançar


def test_policy_write_stdin_valid_with_yield_time_ms(policy):
    """write_stdin com yield_time_ms inteiro passa."""
    call = ToolCall(name="write_stdin", arguments={
        "session_id": 1,
        "yield_time_ms": 100,
    })
    policy.validate(call)


# ── close_command_session policy ────────────────────────────

def test_policy_close_command_session_requires_integer_session_id(policy):
    """close_command_session com session_id não inteiro é rejeitado."""
    call = ToolCall(name="close_command_session", arguments={"session_id": "abc"})
    with pytest.raises(ToolPolicyError, match="session_id inteiro"):
        policy.validate(call)


def test_policy_close_command_session_valid(policy):
    """close_command_session com session_id inteiro passa."""
    call = ToolCall(name="close_command_session", arguments={"session_id": 1})
    policy.validate(call)


# ── exec_command policy ─────────────────────────────────────

def test_policy_exec_command_with_workdir(policy):
    """exec_command com workdir dentro do workspace passa."""
    call = ToolCall(name="exec_command", arguments={
        "cmd": "echo hello",
        "workdir": ".",
    })
    policy.validate(call)


def test_policy_exec_command_with_workdir_outside_workspace(policy):
    """exec_command com workdir fora do workspace é rejeitado."""
    call = ToolCall(name="exec_command", arguments={
        "cmd": "echo hello",
        "workdir": "../../etc",
    })
    with pytest.raises(ToolPolicyError, match="Path fora da workspace"):
        policy.validate(call)


# ── shell policy ────────────────────────────────────────────

def test_policy_shell_chain_operators_all(policy):
    """Todos os operadores de encadeamento são bloqueados."""
    for op in [";", "&&", "||", "`", "$("]:
        call = ToolCall(name="run_shell", arguments={"command": f"ls {op} cat"})
        with pytest.raises(ToolPolicyError, match="operador de encadeamento proibido"):
            policy.validate(call)


def test_policy_shell_allowlist_validation(policy):
    """Comandos na allowlist passam na validação."""
    # 'ls', 'echo', 'cat', etc. estão na allowlist padrão
    call = ToolCall(name="run_shell", arguments={"command": "ls -la"})
    policy.validate(call)  # não deve lançar


# ── blocked_tools ───────────────────────────────────────────

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
    policy = ToolPolicy(config)
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
