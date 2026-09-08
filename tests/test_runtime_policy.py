from pathlib import Path

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import PathPermissionError, ToolPolicyError, is_path_inside
from quimera.runtime.tools.web import WebToolValidator

from tests.helpers import make_policy as _make_policy


# ── validate(): chamadas inválidas (campos obrigatórios, tools desativadas) ──

_REQUIRES_FIELD_REJECTS = [
    pytest.param("unknown", {}, "Sem política", id="unknown-tool"),
    pytest.param("propose_task", {}, "foi desativada", id="propose-task-disabled"),
    pytest.param("apply_patch", {}, "apply_patch requer 'patch'", id="apply-patch-sem-patch"),
    pytest.param("grep_search", {"pattern": ""}, "requer um padrão não vazio", id="grep-sem-pattern"),
    pytest.param("read_file", {}, "requer 'path'", id="read-file-sem-path"),
    pytest.param("read_file", {"path": "../../etc/passwd"}, "Path fora da workspace", id="read-file-fora-workspace"),
    pytest.param("remove_file", {}, "requer 'path'", id="remove-file-sem-path"),
    pytest.param("write_file", {"path": "test.txt"}, "requer 'content'", id="write-file-sem-content"),
    pytest.param("todo_write", {}, "lista não vazia", id="todo-sem-todos"),
    pytest.param("todo_write", {"todos": []}, "lista não vazia", id="todo-todos-vazio"),
]


@pytest.mark.parametrize(("tool", "arguments", "match"), _REQUIRES_FIELD_REJECTS)
def test_policy_validate_rejects_invalid_call(policy, tool, arguments, match):
    with pytest.raises(ToolPolicyError, match=match):
        policy.validate(ToolCall(name=tool, arguments=arguments))


def test_policy_write_file_existing_requires_replace_flag(policy_with_workspace, tmp_path):
    (tmp_path / "test.txt").write_text("old", encoding="utf-8")
    call = ToolCall(name="write_file", arguments={"path": "test.txt", "content": "new"})
    with pytest.raises(ToolPolicyError, match="replace_existing=true"):
        policy_with_workspace.validate(call)


def test_policy_write_file_existing_allowed_with_replace_flag(policy_with_workspace, tmp_path):
    (tmp_path / "test.txt").write_text("old", encoding="utf-8")
    call = ToolCall(
        name="write_file",
        arguments={"path": "test.txt", "content": "new", "replace_existing": True},
    )
    policy_with_workspace.validate(call)


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
    assert policy.requires_approval(ToolCall(name="write_file", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="apply_patch", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="tasks", arguments={})) is True
    assert policy.requires_approval(ToolCall(name="read_file", arguments={})) is False


def test_policy_run_shell_command_alias_removed(policy):
    """Alias removido não é metadata nativa e cai no approval fail-closed."""
    assert "run_shell_command" not in policy._MUTATION_TOOLS
    assert policy.requires_approval(ToolCall(name="run_shell_command", arguments={})) is True


def test_tasks_uses_dedicated_creation_approval_flag(tmp_path):
    """A governança de tasks não depende da flag genérica de mutações."""
    config = ToolRuntimeConfig(
        workspace_root=tmp_path,
        require_approval_for_mutations=True,
        require_approval_for_task_creation=False,
    )
    policy = _make_policy(config)

    assert policy.requires_approval(ToolCall(name="tasks", arguments={})) is False


@pytest.mark.parametrize(
    "method",
    ["POST", "PUT", "PATCH", "DELETE"],
    ids=["post", "put", "patch", "delete"],
)
def test_policy_http_request_mutations_require_approval(policy, method):
    assert policy.requires_approval(
        ToolCall(name="http_request", arguments={"url": "https://example.com", "method": method})
    ) is True


@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD"],
    ids=["get", "head"],
)
def test_policy_http_request_readonly_do_not_require_approval(policy, method):
    assert policy.requires_approval(
        ToolCall(name="http_request", arguments={"url": "https://example.com", "method": method})
    ) is False


def test_policy_other_validations(policy):
    """Cobre os caminhos de pass-through do validate."""
    policy.validate(ToolCall(name="list_tasks", arguments={"job_id": 1}))
    policy.validate(ToolCall(name="list_jobs", arguments={}))
    policy.validate(ToolCall(name="get_job", arguments={}))
    policy.validate(ToolCall(name="memory_retrieve", arguments={}))
    policy.validate(ToolCall(name="todo_list", arguments={}))
    policy.validate(
        ToolCall(name="todo_write", arguments={"todos": [{"content": "task"}]})
    )


def test_policy_disabled_tool_exceptions(policy):
    """approve/complete/fail_task são desativadas no chat."""
    for tool in ["approve_task", "complete_task", "fail_task"]:
        with pytest.raises(ToolPolicyError):
            policy.validate(ToolCall(name=tool, arguments={}))


# ── todo_write policy ─────────────────────────────────────────

_TODO_WRITE_INVALID = [
    pytest.param(["not a dict"], "deve ser um dicionário", id="item-nao-dict"),
    pytest.param([{"priority": "high"}], "requer 'content' não vazio", id="sem-content"),
    pytest.param([{"content": "x", "status": "invalid"}], "status inválido", id="status-invalido"),
    pytest.param([{"content": "x", "priority": "urgent"}], "priority inválida", id="priority-invalida"),
]


@pytest.mark.parametrize(("todos", "match"), _TODO_WRITE_INVALID)
def test_policy_todo_write_rejects_invalid_item(policy, todos, match):
    with pytest.raises(ToolPolicyError, match=match):
        policy.validate(ToolCall(name="todo_write", arguments={"todos": todos}))


def test_policy_todo_write_valid(policy):
    policy.validate(ToolCall(name="todo_write", arguments={"todos": [{"content": "task"}]}))


# ── web_fetch policy ──────────────────────────────────────────

_WEB_FETCH_CASES = [
    pytest.param({"url": "https://example.com"}, None, id="accepts-url"),
    pytest.param({"url": " "}, "url' não vazia", id="rejects-empty-url"),
]


@pytest.mark.parametrize(("arguments", "match"), _WEB_FETCH_CASES)
def test_policy_web_fetch(policy, config, arguments, match):
    policy.register_tool_validator(["web_fetch"], WebToolValidator(config))
    call = ToolCall(name="web_fetch", arguments=arguments)
    if match:
        with pytest.raises(ToolPolicyError, match=match):
            policy.validate(call)
    else:
        policy.validate(call)


# ── remove_file policy ────────────────────────────────────────

_REMOVE_FILE_DRY_RUN = [
    pytest.param({"path": "x.txt"}, "dry_run=False explícito", id="sem-dry-run"),
    pytest.param({"path": "x.txt", "dry_run": True}, "dry_run=False explícito", id="dry-run-true"),
    pytest.param({"path": "x.txt", "dry_run": False}, None, id="dry-run-false"),
    pytest.param({"path": "../../etc/passwd", "dry_run": False}, "Path fora da workspace", id="fora-workspace"),
]


@pytest.mark.parametrize(("arguments", "match"), _REMOVE_FILE_DRY_RUN)
def test_policy_remove_file(policy_with_workspace, tmp_path, arguments, match):
    (tmp_path / "x.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments=arguments)
    if match:
        with pytest.raises(ToolPolicyError, match=match):
            policy_with_workspace.validate(call)
    else:
        policy_with_workspace.validate(call)


def test_policy_remove_file_requires_approval(policy):
    assert policy.requires_approval(ToolCall(name="remove_file", arguments={})) is True


# ── requires_approval expansão ────────────────────────────────

def test_policy_requires_approval_for_all_mutational_tools(policy):
    """Todas as ferramentas mutacionais requerem aprovação."""
    mutational = sorted(policy._MUTATION_TOOLS)
    for tool_name in mutational:
        assert policy.requires_approval(ToolCall(name=tool_name, arguments={})) is True, \
            f"{tool_name} deveria requerer aprovação"


def test_policy_requires_approval_for_replace_text_and_browser_mutators(policy):
    """replace_text e ações de browser mutantes devem passar por approval."""
    for tool_name in (
        "replace_text",
        "browser_close",
        "browser_click",
        "browser_type",
        "browser_evaluate",
        "browser_navigate",
        "git_fetch",
    ):
        assert policy.requires_approval(ToolCall(name=tool_name, arguments={})) is True


def test_write_tools_do_not_offer_external_path_permission(policy):
    """Escritas externas são rejeitadas pelo handler e não devem sugerir grant de path."""
    for tool_name in ("write_file", "replace_text"):
        call = ToolCall(name=tool_name, arguments={"path": "../outside.txt"})
        assert policy.requires_path_permission(call) is False
        assert policy.check_path_permission(call) is None


def test_policy_does_not_require_approval_for_read_tools(policy):
    """Ferramentas de leitura não requerem aprovação."""
    readonly = [
        "read_file",
        "list_files",
        "grep_search",
        "list_tasks",
        "list_jobs",
        "get_job",
        "memory_save",
        "memory_retrieve",
        "todo_list",
        "todo_write",
        "web_search",
        "web_fetch",
        "git_status",
        "browser_status",
        "browser_snapshot",
        "ask_user",
        "update_shared_state",
        "list_agents",
    ]
    for tool_name in readonly:
        assert policy.requires_approval(ToolCall(name=tool_name, arguments={})) is False, \
            f"{tool_name} NÃO deveria requerer aprovação"


def test_policy_check_path_permission_none_for_non_path_tools(policy):
    """check_path_permission retorna None para ferramentas que não operam em paths."""
    non_path_tools = [
        "run_shell",
        "write_file",
        "replace_text",
        "apply_patch",
        "write_stdin",
    ]
    for tool_name in non_path_tools:
        call = ToolCall(name=tool_name, arguments={})
        assert policy.check_path_permission(call) is None


# ── memory / shared_state policy ──────────────────────────────

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


def test_policy_update_shared_state_valid_payload_does_not_run_shell_validation(policy):
    policy.validate(
        ToolCall(name="update_shared_state", arguments={"updates": {"phase": "x"}})
    )


def test_policy_memory_delete_requires_approval(policy):
    assert policy.requires_approval(
        ToolCall(name="memory_delete", arguments={"namespace": "workspace"})
    ) is True


# ── shell policy: rejeições e aceites do ShellToolValidator ───

_SHELL_REJECTS = [
    pytest.param("write_stdin", {}, "write_stdin requer 'session_id'", id="stdin-sem-session"),
    pytest.param("close_command_session", {}, "close_command_session requer 'session_id'", id="close-sem-session"),
    pytest.param("poll_command_session", {}, "poll_command_session requer 'session_id'", id="poll-sem-session"),
    pytest.param("write_stdin", {"session_id": "abc"}, "session_id inteiro", id="stdin-session-nao-int"),
    pytest.param("close_command_session", {"session_id": "abc"}, "session_id inteiro", id="close-session-nao-int"),
    pytest.param("write_stdin", {"session_id": 1, "yield_time_ms": "abc"}, "yield_time_ms inteiro", id="stdin-yield-nao-int"),
    pytest.param("run_shell", {"command": "  "}, "run_shell requer um comando não vazio", id="run-shell-vazio"),
    pytest.param("exec_command", {"cmd": "  "}, "exec_command requer um comando não vazio", id="exec-vazio"),
    pytest.param("run_shell", {"command": "rm -rf /"}, "Comando bloqueado", id="run-shell-denylist"),
    pytest.param("run_shell", {"command": "ls && cat"}, "operador de encadeamento proibido", id="run-shell-chain"),
    pytest.param("run_shell", {"command": 'echo "unclosed quote'}, "Comando inválido", id="run-shell-shlex-invalido"),
    pytest.param("run_shell", {"command": "nc -l 8080"}, "fora da allowlist", id="run-shell-fora-allowlist"),
    pytest.param("exec_command", {"cmd": "echo hello", "workdir": "../../etc"}, "Path fora da workspace", id="exec-workdir-fora"),
    pytest.param("run_shell", {"command": "cat /etc/passwd"}, "fora do workspace", id="run-shell-cat-absoluto-fora"),
    pytest.param("run_shell", {"command": "cat ~/.ssh/id_rsa"}, "fora do workspace", id="run-shell-cat-tilde-fora"),
]


@pytest.mark.parametrize(("tool", "arguments", "match"), _SHELL_REJECTS)
def test_policy_shell_validator_rejects(shell_validator, tool, arguments, match):
    with pytest.raises(ToolPolicyError, match=match):
        shell_validator.validate(ToolCall(name=tool, arguments=arguments))


_SHELL_ACCEPTS = [
    pytest.param("run_shell", {"command": "ls -la"}, id="allowlist"),
    pytest.param("run_shell", {"command": "cat requirements.txt"}, id="cat-relativo"),
    pytest.param("run_shell", {"command": "echo /etc/passwd"}, id="nao-file-cmd"),
    pytest.param("write_stdin", {"session_id": 1}, id="stdin-valido"),
    pytest.param("write_stdin", {"session_id": 1, "yield_time_ms": 100}, id="stdin-com-yield"),
    pytest.param("close_command_session", {"session_id": 1}, id="close-valido"),
    pytest.param("poll_command_session", {"session_id": 1, "yield_time_ms": 100}, id="poll-valido"),
    pytest.param("exec_command", {"cmd": "echo hello", "workdir": "."}, id="exec-com-workdir"),
    pytest.param("exec_command", {"cmd": "sed -i 's/403/401/' tests/example.py"}, id="sed-in-place"),
    pytest.param("exec_command", {"cmd": "python -c 'print(1); print(2)'"}, id="ponto-e-virgula-em-python"),
]


@pytest.mark.parametrize(("tool", "arguments"), _SHELL_ACCEPTS)
def test_policy_shell_validator_accepts(shell_validator, tool, arguments):
    shell_validator.validate(ToolCall(name=tool, arguments=arguments))


def test_policy_shell_allows_workspace_venv_executable(shell_validator_with_workspace, tmp_path):
    """Permite executável dentro da workspace quando o basename está na allowlist."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    pytest_bin = venv_bin / "pytest"
    pytest_bin.write_text("#!/bin/sh\n")
    call = ToolCall(
        name="exec_command",
        arguments={"cmd": ".venv/bin/pytest tests/test_example.py"},
    )

    shell_validator_with_workspace.validate(call)


def test_policy_shell_allows_workspace_venv_python_symlink(shell_validator_with_workspace, tmp_path):
    """Permite symlink de executável do venv quando o link está dentro da workspace."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_bin = venv_bin / "python"
    python_bin.symlink_to("/usr/bin/python3")
    call = ToolCall(
        name="exec_command",
        arguments={"cmd": ".venv/bin/python -m pytest -q"},
    )

    shell_validator_with_workspace.validate(call)


def test_policy_shell_file_cmd_absolute_inside_workspace(shell_validator_with_workspace, tmp_path):
    """cat com path absoluto dentro do workspace é permitido."""
    target = tmp_path / "file.txt"
    target.write_text("data")
    call = ToolCall(name="run_shell", arguments={"command": f"cat {target}"})
    shell_validator_with_workspace.validate(call)


def test_policy_run_shell_allows_mkdir_inside_workspace(shell_validator_with_workspace, tmp_path):
    """mkdir é permitido no shell, mas restrito ao workspace."""
    call = ToolCall(name="run_shell", arguments={"command": "mkdir -p nova/pasta"})
    shell_validator_with_workspace.validate(call)


_MKDIR_BLOCKS = [
    pytest.param("mkdir ../fora", "Caminho fora do workspace", id="fora-workspace"),
    pytest.param("mkdir -m 777 pasta", "Flag não permitida", id="flag-desautorizada"),
]


@pytest.mark.parametrize(("command", "match"), _MKDIR_BLOCKS)
def test_policy_run_shell_blocks_mkdir(shell_validator_with_workspace, tmp_path, command, match):
    call = ToolCall(name="run_shell", arguments={"command": command})
    with pytest.raises(ToolPolicyError, match=match):
        shell_validator_with_workspace.validate(call)


def test_policy_shell_chain_operators_all(shell_validator):
    """Todos os operadores de encadeamento são bloqueados."""
    for op in [";", "&&", "||", "|", "`", "$("]:
        call = ToolCall(name="run_shell", arguments={"command": f"ls {op} cat"})
        with pytest.raises(ToolPolicyError, match="operador de encadeamento proibido"):
            shell_validator.validate(call)


def test_policy_shell_blocks_chain_operators_without_spaces(shell_validator):
    """Bloqueia operadores de shell mesmo quando grudados em argumentos."""
    for command in [
        "echo ok;cat /etc/passwd",
        "echo ok|cat",
        "echo ok&&cat",
        "echo ok||cat",
    ]:
        call = ToolCall(name="exec_command", arguments={"cmd": command})
        with pytest.raises(ToolPolicyError, match="operador de encadeamento proibido"):
            shell_validator.validate(call)


# ── delegate policy ───────────────────────────────────────────

_DELEGATE_VALID = [
    {"target_agent": "codex", "request": "faça algo"},
    {
        "target_agent": "codex",
        "request": "faça algo",
        "steps": [{"target_agent": "opencode", "request": "próximo passo"}],
    },
    {
        "target_agent": "codex",
        "request": "faça algo",
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
]

_DELEGATE_INVALID = [
    ({"request": "faça algo"}, "target_agent"),
    ({"target_agent": "codex"}, "request"),
    ({"target_agent": "codex", "request": "faça algo", "run_id": "abc"}, "campos reservados"),
    ({"target_agent": "codex", "request": "faça algo", "steps": "não é lista"}, "steps deve ser uma lista"),
    ({"target_agent": "codex", "request": "faça algo", "steps": ["string"]}, r"steps\[0\] deve ser um objeto"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"request": "próximo passo"}]}, r"steps\[0\].target_agent"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "opencode"}]}, r"steps\[0\].request"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "  ", "request": "algo"}]}, r"steps\[0\].target_agent"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "opencode", "request": ""}]}, r"steps\[0\].request"),
    ({"target_agent": "codex", "request": "faça algo", "role": "worker"}, "delegate.role"),
    ({"target_agent": "codex", "request": "faça algo", "access_list": ["diff", " "]}, r"delegate.access_list\[1\]"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "claude", "request": "revise", "role": "worker"}]}, r"delegate.steps\[0\].role"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "claude", "request": "revise", "access_list": "diff"}]}, r"delegate.steps\[0\].access_list"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "claude", "request": "revise", "access_list": ["diff", "  "]}]}, r"delegate.steps\[0\].access_list\[1\]"),
    ({"target_agent": "codex", "request": "faça algo", "steps": [{"target_agent": "opencode", "request": "passo 1"}, {"target_agent": "opencode"}]}, r"steps\[1\].request"),
]


@pytest.mark.parametrize("arguments", _DELEGATE_VALID)
def test_policy_delegate_accepts_valid_payload(policy, arguments):
    """delegate com payload válido (mínimo, steps e role/access_list) passa."""
    policy.validate(ToolCall(name="delegate", arguments=arguments))


@pytest.mark.parametrize(("arguments", "match"), _DELEGATE_INVALID)
def test_policy_delegate_rejects_invalid_payload(policy, arguments, match):
    """delegate com payload inválido é rejeitado com a mensagem esperada."""
    with pytest.raises(ToolPolicyError, match=match):
        policy.validate(ToolCall(name="delegate", arguments=arguments))


def test_policy_delegate_blocked_tools(policy):
    """delegate é bloqueado quando na lista blocked_tools."""
    policy.blocked_tools = ["delegate"]
    call = ToolCall(name="delegate", arguments={"target_agent": "codex", "request": "x"})
    with pytest.raises(ToolPolicyError, match="bloqueada pelo modo de execução ativo"):
        policy.validate(call)


def test_policy_delegate_requires_approval(policy):
    assert policy.requires_approval(ToolCall(name="delegate", arguments={})) is True


def test_policy_blocked_tools(policy):
    """Ferramentas na lista blocked_tools são rejeitadas."""
    policy.blocked_tools = ["list_files"]
    call = ToolCall(name="list_files", arguments={})
    with pytest.raises(ToolPolicyError, match="bloqueada pelo modo de execução ativo"):
        policy.validate(call)


def test_policy_allowed_tools_rejects_everything_outside_allowlist(policy):
    policy.allowed_tools = ["list_files"]

    policy.validate(ToolCall(name="list_files", arguments={"path": "."}))
    with pytest.raises(ToolPolicyError, match="nao permitida"):
        policy.validate(ToolCall(name="read_file", arguments={"path": "README.md"}))


# ── _resolve_workspace_path ───────────────────────────────────

def test_policy_resolve_workspace_path_empty_becomes_current(policy_with_workspace):
    """Path vazio resolve para '.' (diretório corrente)."""
    call = ToolCall(name="list_files", arguments={"path": ""})
    policy_with_workspace.validate(call)


# ── PathPermissionError ───────────────────────────────────────

def test_path_permission_error_attributes():
    """PathPermissionError guarda raw_path e resolved_path."""
    raw = "/etc/shadow"
    resolved = Path("/etc/shadow")
    exc = PathPermissionError(raw, resolved)
    assert exc.raw_path == raw
    assert exc.resolved_path == resolved
    assert "Permissão necessária" in str(exc)


# ── check_path_permission ─────────────────────────────────────

_CHECK_PATH_PERMISSION = [
    pytest.param("remove_file", {"path": ".", "dry_run": False}, False, id="remove-dentro"),
    pytest.param("remove_file", {"path": "../../etc/passwd", "dry_run": False}, True, id="remove-fora"),
    pytest.param("list_files", {"path": "."}, False, id="list-dentro"),
    pytest.param("grep_search", {"pattern": "test"}, False, id="grep-dentro"),
    pytest.param("grep_search", {"pattern": "test", "path": "../../etc/passwd"}, True, id="grep-fora"),
    pytest.param("list_files", {"path": "../../etc"}, True, id="list-fora"),
]


@pytest.mark.parametrize(("tool", "arguments", "outside"), _CHECK_PATH_PERMISSION)
def test_policy_check_path_permission(policy, tool, arguments, outside):
    result = policy.check_path_permission(ToolCall(name=tool, arguments=arguments))
    if outside:
        assert result is not None
    else:
        assert result is None


def test_policy_check_path_permission_for_remove_file_outside(policy):
    """check_path_permission para remove_file fora dos roots expõe o path resolvido."""
    call = ToolCall(name="remove_file", arguments={"path": "../../etc/passwd", "dry_run": False})
    result = policy.check_path_permission(call)
    assert result is not None
    assert "etc" in str(result.resolved_path)


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


# ── is_path_inside ────────────────────────────────────────────

def test_is_path_inside_same_dir(tmp_path):
    assert is_path_inside(tmp_path / "file.txt", tmp_path) is True


def test_is_path_inside_subdir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    assert is_path_inside(sub / "file.txt", tmp_path) is True


def test_is_path_inside_outside(tmp_path):
    assert is_path_inside(Path("/etc/passwd"), tmp_path) is False


def test_is_path_inside_prefix_sibling(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace2"
    sibling.mkdir()
    assert is_path_inside(sibling / "secret.txt", workspace) is False


def test_is_path_inside_exact_root(tmp_path):
    assert is_path_inside(tmp_path, tmp_path) is True


def test_is_path_inside_symlink(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "target.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert is_path_inside(link, tmp_path) is True


def test_is_path_inside_with_symlinked_root(tmp_path):
    real_root = tmp_path / "workspace"
    real_root.mkdir()
    root_alias = tmp_path / "workspace-link"
    root_alias.symlink_to(real_root, target_is_directory=True)

    child = real_root / "file.txt"
    assert is_path_inside(child, root_alias) is True