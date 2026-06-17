"""Testes para quimera.runtime.tools.git (GitTool)."""
from __future__ import annotations

import subprocess

import pytest

from quimera.runtime.approval import AutoApprovalHandler
from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.executor import ToolExecutor
from quimera.runtime.models import ToolCall
from quimera.runtime.policy import ToolPolicy, ToolPolicyError
from quimera.runtime.tools.git import GitTool, GitToolValidator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """Cria um repositório git mínimo em tmp_path e retorna o path."""
    env = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com",
           "HOME": str(tmp_path)}

    def git(*args):
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            env={**__import__("os").environ, **env},
        )

    git("init")
    git("config", "user.email", "test@test.com")
    git("config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hello")
    git("add", ".")
    git("commit", "-m", "initial commit")
    return tmp_path


@pytest.fixture
def config(git_repo):
    return ToolRuntimeConfig(workspace_root=git_repo)


@pytest.fixture
def tool(config):
    return GitTool(config)


_GIT_TOOL_NAMES = [
    "git_status", "git_log", "git_diff", "git_branch", "git_fetch",
    "git_add", "git_commit", "git_checkout", "git_push",
]


@pytest.fixture
def policy(config):
    p = ToolPolicy(config)
    p.register_tool_validator(_GIT_TOOL_NAMES, GitToolValidator(config))
    return p


@pytest.fixture
def executor(config):
    return ToolExecutor(config, AutoApprovalHandler())


def _call(name, **kwargs):
    return ToolCall(name=name, arguments=kwargs)


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


def test_git_status_clean(tool):
    result = tool.git_status(_call("git_status"))
    assert result.ok
    assert result.data["clean"] is True
    assert "branch:" in result.content


def test_git_status_untracked(tool, config):
    (config.workspace_root / "new.py").write_text("x = 1")
    result = tool.git_status(_call("git_status"))
    assert result.ok
    assert "new.py" in result.data["untracked"]
    assert result.data["clean"] is False


def test_git_status_staged(tool, config):
    (config.workspace_root / "new.py").write_text("x = 1")
    tool._run_git(["add", "new.py"])
    result = tool.git_status(_call("git_status"))
    assert result.ok
    staged_paths = [s["path"] for s in result.data["staged"]]
    assert "new.py" in staged_paths


def test_git_status_unstaged(tool, config):
    (config.workspace_root / "README.md").write_text("modified")
    result = tool.git_status(_call("git_status"))
    assert result.ok
    unstaged_paths = [s["path"] for s in result.data["unstaged"]]
    assert "README.md" in unstaged_paths


def test_git_status_not_a_repo(tmp_path):
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    tool = GitTool(config)
    result = tool.git_status(_call("git_status"))
    assert not result.ok


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------


def test_git_log_returns_commits(tool):
    result = tool.git_log(_call("git_log"))
    assert result.ok
    commits = result.data["commits"]
    assert len(commits) >= 1
    assert commits[0]["message"] == "initial commit"
    assert len(commits[0]["hash"]) == 40
    assert len(commits[0]["short_hash"]) == 7


def test_git_log_max_count(tool, config):
    for i in range(5):
        (config.workspace_root / f"f{i}.txt").write_text(str(i))
        tool._run_git(["add", f"f{i}.txt"])
        tool._run_git(["commit", "-m", f"commit {i}"])

    result = tool.git_log(_call("git_log", max_count=3))
    assert result.ok
    assert len(result.data["commits"]) == 3


def test_git_log_empty_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
    config = ToolRuntimeConfig(workspace_root=tmp_path)
    tool = GitTool(config)
    result = tool.git_log(_call("git_log"))
    # git log em repo vazio retorna erro (no commits yet)
    assert not result.ok or result.data["commits"] == []


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


def test_git_diff_clean(tool):
    result = tool.git_diff(_call("git_diff"))
    assert result.ok
    assert "no changes" in result.content


def test_git_diff_unstaged(tool, config):
    (config.workspace_root / "README.md").write_text("modified content")
    result = tool.git_diff(_call("git_diff"))
    assert result.ok
    assert "README.md" in result.content


def test_git_diff_staged(tool, config):
    (config.workspace_root / "README.md").write_text("staged content")
    tool._run_git(["add", "README.md"])
    result = tool.git_diff(_call("git_diff", staged=True))
    assert result.ok
    assert "README.md" in result.content


def test_git_diff_ref(tool, config):
    (config.workspace_root / "f.py").write_text("x = 1")
    tool._run_git(["add", "f.py"])
    tool._run_git(["commit", "-m", "add f.py"])
    result = tool.git_diff(_call("git_diff", ref1="HEAD~1", ref2="HEAD"))
    assert result.ok
    assert "f.py" in result.content


# ---------------------------------------------------------------------------
# git_branch
# ---------------------------------------------------------------------------


def test_git_branch_list(tool):
    result = tool.git_branch(_call("git_branch"))
    assert result.ok
    names = [b["name"] for b in result.data["branches"]]
    assert any(n in ("main", "master") for n in names)
    assert result.data["current"] in names


def test_git_branch_shows_current(tool):
    result = tool.git_branch(_call("git_branch"))
    current = result.data["current"]
    current_entries = [b for b in result.data["branches"] if b["current"]]
    assert len(current_entries) == 1
    assert current_entries[0]["name"] == current


# ---------------------------------------------------------------------------
# git_add
# ---------------------------------------------------------------------------


def test_git_add_specific_file(tool, config):
    (config.workspace_root / "added.txt").write_text("hello")
    result = tool.git_add(_call("git_add", paths="added.txt"))
    assert result.ok
    assert "added.txt" in result.data["staged"]


def test_git_add_all(tool, config):
    (config.workspace_root / "a.txt").write_text("a")
    (config.workspace_root / "b.txt").write_text("b")
    result = tool.git_add(_call("git_add"))
    assert result.ok
    staged = result.data["staged"]
    assert "a.txt" in staged
    assert "b.txt" in staged


def test_git_add_list_of_paths(tool, config):
    (config.workspace_root / "x.txt").write_text("x")
    result = tool.git_add(_call("git_add", paths=["x.txt"]))
    assert result.ok
    assert "x.txt" in result.data["staged"]


def test_git_add_nonexistent_file(tool):
    result = tool.git_add(_call("git_add", paths="nonexistent_file_xyz.txt"))
    assert not result.ok


# ---------------------------------------------------------------------------
# git_commit
# ---------------------------------------------------------------------------


def test_git_commit_creates_commit(tool, config):
    (config.workspace_root / "c.txt").write_text("c")
    tool.git_add(_call("git_add", paths="c.txt"))
    result = tool.git_commit(_call("git_commit", message="add c.txt"))
    assert result.ok
    assert len(result.data["commit"]) == 40
    assert result.data["message"] == "add c.txt"
    assert "add c.txt" in result.content


def test_git_commit_nothing_staged(tool):
    result = tool.git_commit(_call("git_commit", message="empty"))
    assert not result.ok


# ---------------------------------------------------------------------------
# git_checkout
# ---------------------------------------------------------------------------


def test_git_checkout_create_branch(tool):
    result = tool.git_checkout(_call("git_checkout", branch="feature/x", create=True))
    assert result.ok
    assert result.data["created"] is True
    assert result.data["branch"] == "feature/x"

    status = tool.git_status(_call("git_status"))
    assert status.data["branch"] == "feature/x"


def test_git_checkout_existing_branch(tool):
    tool._run_git(["branch", "existing-branch"])
    result = tool.git_checkout(_call("git_checkout", branch="existing-branch"))
    assert result.ok
    assert result.data["branch"] == "existing-branch"


def test_git_checkout_nonexistent_branch(tool):
    result = tool.git_checkout(_call("git_checkout", branch="nonexistent-xyz"))
    assert not result.ok


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


def test_policy_git_log_invalid_max_count(policy):
    call = _call("git_log", max_count="not-a-number")
    with pytest.raises(ToolPolicyError, match="max_count"):
        policy.validate(call)


def test_policy_git_log_invalid_branch(policy):
    call = _call("git_log", branch="feat; rm -rf /")
    with pytest.raises(ToolPolicyError, match="branch"):
        policy.validate(call)


def test_policy_git_diff_path_outside_workspace(policy, config):
    call = _call("git_diff", path="../../etc/passwd")
    with pytest.raises(ToolPolicyError, match="Path fora da workspace|fora do workspace"):
        policy.validate(call)


def test_policy_git_diff_invalid_ref(policy):
    call = _call("git_diff", ref1="main; echo injected")
    with pytest.raises(ToolPolicyError, match="ref1"):
        policy.validate(call)


def test_policy_git_commit_empty_message(policy):
    call = _call("git_commit", message="")
    with pytest.raises(ToolPolicyError, match="message"):
        policy.validate(call)


def test_policy_git_commit_missing_message(policy):
    call = _call("git_commit")
    with pytest.raises(ToolPolicyError, match="message"):
        policy.validate(call)


def test_policy_git_checkout_empty_branch(policy):
    call = _call("git_checkout", branch="")
    with pytest.raises(ToolPolicyError, match="branch"):
        policy.validate(call)


def test_policy_git_checkout_invalid_branch(policy):
    call = _call("git_checkout", branch="feat; evil")
    with pytest.raises(ToolPolicyError, match="branch"):
        policy.validate(call)


def test_policy_git_push_invalid_remote(policy):
    call = _call("git_push", remote="origin; evil")
    with pytest.raises(ToolPolicyError, match="remote"):
        policy.validate(call)


def test_policy_git_fetch_invalid_remote(policy):
    call = _call("git_fetch", remote="bad remote!")
    with pytest.raises(ToolPolicyError, match="remote"):
        policy.validate(call)


def test_policy_requires_approval_for_mutations(policy):
    for name in ("git_add", "git_commit", "git_checkout", "git_push"):
        call = _call(name, message="m", branch="main", remote="origin")
        assert policy.requires_approval(call) == policy.config.require_approval_for_mutations


def test_policy_no_approval_for_read_ops(policy):
    for name in ("git_status", "git_log", "git_diff", "git_branch", "git_fetch"):
        call = _call(name)
        assert not policy.requires_approval(call)


# ---------------------------------------------------------------------------
# Executor integration (via AutoApprovalHandler — sem interação humana)
# ---------------------------------------------------------------------------


def test_executor_git_status_registered(executor):
    result = executor.execute(_call("git_status"))
    assert result.ok


def test_executor_git_log_registered(executor):
    result = executor.execute(_call("git_log"))
    assert result.ok


def test_executor_git_branch_registered(executor):
    result = executor.execute(_call("git_branch"))
    assert result.ok


def test_executor_git_add_commit_cycle(executor, config):
    (config.workspace_root / "cycle.txt").write_text("content")
    add_result = executor.execute(_call("git_add", paths="cycle.txt"))
    assert add_result.ok
    commit_result = executor.execute(_call("git_commit", message="cycle commit"))
    assert commit_result.ok
    assert len(commit_result.data["commit"]) == 40


def test_mcp_tool_schemas_include_git():
    from quimera.runtime.drivers.tool_schemas import TOOL_SCHEMAS
    git_names = {
        s["function"]["name"]
        for s in TOOL_SCHEMAS
        if s["function"]["name"].startswith("git_")
    }
    expected = {"git_status", "git_log", "git_diff", "git_branch", "git_fetch",
                "git_add", "git_commit", "git_checkout", "git_push"}
    assert expected == git_names
