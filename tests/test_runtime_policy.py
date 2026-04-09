import pytest
from pathlib import Path
from quimera.runtime.policy import ToolPolicy, ToolPolicyError
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall

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
