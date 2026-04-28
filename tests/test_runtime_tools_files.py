from unittest.mock import patch

import pytest

from quimera.runtime.config import ToolRuntimeConfig
from quimera.runtime.models import ToolCall
from quimera.runtime.tools.files import FileTools, set_staging_root


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return ToolRuntimeConfig(workspace_root=root)


@pytest.fixture
def tools(config):
    return FileTools(config)


def test_file_tools_resolve_outside(tools):
    # Line 36 coverage
    with pytest.raises(ValueError, match="Path fora da workspace"):
        tools._resolve("../../etc/passwd")


def test_file_tools_list_files_staging(tools, config):
    # Line 47, 58-61 coverage
    workspace = config.workspace_root
    (workspace / "a.txt").write_text("a")
    (workspace / "subdir").mkdir()
    (workspace / "subdir/b.txt").write_text("b")

    staging = workspace.parent / "staging"
    staging.mkdir()
    (staging / "c.txt").write_text("c")
    (staging / "subdir").mkdir()
    (staging / "subdir/d.txt").write_text("d")

    set_staging_root(staging)
    try:
        # list root
        call = ToolCall(name="list_files", arguments={"path": "."})
        result = tools.list_files(call)
        assert "c.txt" in result.content

        # list subdir
        call = ToolCall(name="list_files", arguments={"path": "subdir"})
        result = tools.list_files(call)
        assert "d.txt" in result.content
    finally:
        set_staging_root(None)


def test_file_tools_read_file_staging(tools, config):
    workspace = config.workspace_root
    (workspace / "a.txt").write_text("original")

    staging = workspace.parent / "staging"
    staging.mkdir()
    (staging / "a.txt").write_text("staged")

    set_staging_root(staging)
    try:
        call = ToolCall(name="read_file", arguments={"path": "a.txt"})
        result = tools.read_file(call)
        assert result.content == "staged"
    finally:
        set_staging_root(None)


def test_file_tools_write_file_modes(tools, config):
    # Line 97, 99-100 coverage
    workspace = config.workspace_root
    path = workspace / "test.txt"

    # overwrite
    tools.write_file(ToolCall(name="write_file", arguments={"path": "test.txt", "content": "hello"}))
    assert path.read_text() == "hello"

    # create existing
    result = tools.write_file(
        ToolCall(name="write_file", arguments={"path": "test.txt", "content": "hi", "mode": "create"}))
    assert result.ok is False
    assert "já existe" in result.error

    # append
    tools.write_file(ToolCall(name="write_file", arguments={"path": "test.txt", "content": " world", "mode": "append"}))
    assert path.read_text() == "hello world"


def test_file_tools_write_file_overwrite_requires_replace_existing(tools, config):
    workspace = config.workspace_root
    path = workspace / "test.txt"
    path.write_text("hello")

    blocked = tools.write_file(ToolCall(name="write_file", arguments={"path": "test.txt", "content": "changed"}))
    assert blocked.ok is False
    assert "replace_existing=true" in blocked.error
    assert path.read_text() == "hello"

    allowed = tools.write_file(
        ToolCall(name="write_file", arguments={"path": "test.txt", "content": "changed", "replace_existing": True}))
    assert allowed.ok is True
    assert path.read_text() == "changed"


def test_file_tools_grep_search_staging(tools, config):
    # Line 114-116 coverage
    workspace = config.workspace_root
    (workspace / "a.txt").write_text("foo")

    staging = workspace.parent / "staging"
    staging.mkdir()
    (staging / "b.txt").write_text("foo staged")

    set_staging_root(staging)
    try:
        call = ToolCall(name="grep_search", arguments={"pattern": "foo"})
        result = tools.grep_search(call)
        assert "a.txt" in result.content
        assert "b.txt" in result.content
    finally:
        set_staging_root(None)


def test_file_tools_grep_search_error(tools, config):
    # Line 126 coverage
    workspace = config.workspace_root
    (workspace / "a.txt").write_text("foo")

    with patch("pathlib.Path.read_text") as mock_read:
        mock_read.side_effect = Exception("Boom")
        call = ToolCall(name="grep_search", arguments={"pattern": "foo"})
        result = tools.grep_search(call)
        assert result.ok is True
        assert result.content == ""


# ── remove_file ──────────────────────────────────────────────

def test_remove_file_dry_run_default(tools, config):
    """dry_run=True por padrão: não remove, apenas reporta."""
    workspace = config.workspace_root
    (workspace / "to_delete.txt").write_text("bye")

    call = ToolCall(name="remove_file", arguments={"path": "to_delete.txt"})
    result = tools.remove_file(call)

    assert result.ok is True
    assert "[dry-run]" in result.content
    assert (workspace / "to_delete.txt").exists()


def test_remove_file_actual_deletion(tools, config):
    """Com dry_run=False, o arquivo é removido de fato."""
    workspace = config.workspace_root
    (workspace / "to_delete.txt").write_text("bye")

    call = ToolCall(name="remove_file", arguments={"path": "to_delete.txt", "dry_run": False})
    result = tools.remove_file(call)

    assert result.ok is True
    assert "removido" in result.content.lower()
    assert not (workspace / "to_delete.txt").exists()


def test_remove_file_not_found(tools, config):
    """Arquivo inexistente retorna erro."""
    call = ToolCall(name="remove_file", arguments={"path": "nao_existe.txt"})
    result = tools.remove_file(call)

    assert result.ok is False
    assert "não encontrado" in result.error.lower()


def test_remove_file_refuses_directory(tools, config):
    """remove_file não remove diretórios."""
    workspace = config.workspace_root
    (workspace / "subdir").mkdir()

    call = ToolCall(name="remove_file", arguments={"path": "subdir", "dry_run": False})
    result = tools.remove_file(call)

    assert result.ok is False
    assert "não remove diretórios" in result.error.lower()


def test_remove_file_refuses_non_regular(tools, config):
    """remove_file recusa algo que não é arquivo regular."""
    workspace = config.workspace_root
    (workspace / "fifo").touch()

    call = ToolCall(name="remove_file", arguments={"path": "fifo", "dry_run": False})
    with patch("pathlib.Path.is_file", return_value=False):
        result = tools.remove_file(call)

    assert result.ok is False
    assert "não é um arquivo regular" in result.error.lower()


def test_remove_file_os_error(tools, config):
    """Erro de sistema (ex: permissão) retorna erro."""
    workspace = config.workspace_root
    (workspace / "protected.txt").write_text("x")

    call = ToolCall(name="remove_file", arguments={"path": "protected.txt", "dry_run": False})
    with patch("pathlib.Path.unlink", side_effect=OSError("Permission denied")):
        result = tools.remove_file(call)

    assert result.ok is False
    assert "Permission denied" in result.error


def test_remove_file_outside_workspace(tools, config):
    """Path fora do workspace é rejeitado por _resolve."""
    call = ToolCall(name="remove_file", arguments={"path": "../../etc/passwd", "dry_run": False})
    with pytest.raises(ValueError, match="Path fora da workspace"):
        tools.remove_file(call)
