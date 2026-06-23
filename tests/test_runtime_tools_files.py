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
    """Verifica que _resolve rejeita path fora da workspace."""
    # Line 36 coverage
    with pytest.raises(ValueError, match="Path fora da workspace"):
        tools._resolve("../../etc/passwd")


def test_file_tools_read_file_rejects_prefix_sibling_path(tools, config):
    """Bloqueia bypass por prefixo de path (workspace vs workspace2)."""
    sibling = config.workspace_root.parent / f"{config.workspace_root.name}2"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("TOPSECRET", encoding="utf-8")

    with pytest.raises(ValueError, match="Path fora da workspace"):
        tools.read_file(
            ToolCall(
                name="read_file",
                arguments={"path": f"../{sibling.name}/secret.txt"},
            )
        )


def test_file_tools_list_files_staging(tools, config):
    """Verifica que list_files inclui arquivos do staging."""
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
    """Verifica que read_file prioriza arquivo do staging sobre workspace."""
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
    """Verifica que write_file suporta modos overwrite, create e append."""
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
    """Verifica que sobrescrita de arquivo existente exige replace_existing=true."""
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


def test_write_file_does_not_mutate_allowed_read_root(tmp_path):
    """allowed_read_roots são leitura; mutações continuam limitadas à workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    read_root = tmp_path / "read-root"
    read_root.mkdir()
    config = ToolRuntimeConfig(
        workspace_root=workspace,
        allowed_read_roots=[workspace, read_root],
    )
    tools = FileTools(config)

    call = ToolCall(
        name="write_file",
        arguments={"path": f"../{read_root.name}/created.txt", "content": "x"},
    )
    with pytest.raises(ValueError, match="Path fora da workspace"):
        tools.write_file(call)
    assert not (read_root / "created.txt").exists()


def test_file_tools_grep_search_staging(tools, config):
    """Verifica que grep_search busca também no staging."""
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
    """Verifica que grep_search trata erro de leitura silenciosamente."""
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


def test_remove_file_does_not_mutate_allowed_read_root(tmp_path):
    """remove_file também não opera em allowed_read_roots fora da workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    read_root = tmp_path / "read-root"
    read_root.mkdir()
    protected = read_root / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    config = ToolRuntimeConfig(
        workspace_root=workspace,
        allowed_read_roots=[workspace, read_root],
    )
    tools = FileTools(config)

    call = ToolCall(
        name="remove_file",
        arguments={"path": f"../{read_root.name}/protected.txt", "dry_run": False},
    )
    with pytest.raises(ValueError, match="Path fora da workspace"):
        tools.remove_file(call)
    assert protected.exists()


# ── read_file range de linhas ─────────────────────────────────

def test_read_file_range_start_only(tools, config):
    """read_file com start_line lê da linha em diante."""
    workspace = config.workspace_root
    lines = ["a", "b", "c", "d", "e"]
    (workspace / "test.txt").write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": 3})
    result = tools.read_file(call)
    assert result.ok is True
    assert result.content == "c\nd\ne\n"


def test_read_file_range_start_end(tools, config):
    """read_file com start_line e end_line (inclusivo) lê intervalo."""
    workspace = config.workspace_root
    lines = ["a", "b", "c", "d", "e"]
    (workspace / "test.txt").write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": 2, "end_line": 4})
    result = tools.read_file(call)
    assert result.ok is True
    # end_line é inclusivo: lines[start-1:end_line] → lines[1:4] → b, c, d
    assert result.content == "b\nc\nd\n"


def test_read_file_range_end_greater_than_total(tools, config):
    """read_file com end_line > total limita ao total."""
    workspace = config.workspace_root
    lines = ["a", "b", "c"]
    (workspace / "test.txt").write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": 2, "end_line": 99})
    result = tools.read_file(call)
    assert result.ok is True
    assert result.content == "b\nc\n"


def test_read_file_range_negative_start_clamps(tools, config):
    """read_file com start_line < 1 é tratado como 0 (início)."""
    workspace = config.workspace_root
    lines = ["a", "b", "c"]
    (workspace / "test.txt").write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": -5, "end_line": 2})
    result = tools.read_file(call)
    assert result.ok is True
    assert result.content == "a\nb\n"


def test_read_file_range_invalid_start_ge_end(tools, config):
    """read_file com start_line >= end_line retorna erro."""
    workspace = config.workspace_root
    lines = ["a", "b", "c"]
    (workspace / "test.txt").write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": 3, "end_line": 2})
    result = tools.read_file(call)
    assert result.ok is False
    assert "Intervalo inválido" in result.error


def test_read_file_range_end_line_zero(tools, config):
    """read_file com end_line=0 retorna erro (start=1 >= end=0)."""
    workspace = config.workspace_root
    (workspace / "test.txt").write_text("a\nb\nc\n", encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": 1, "end_line": 0})
    result = tools.read_file(call)
    assert result.ok is False
    assert "Intervalo inválido" in result.error


def test_read_file_range_invalid_start_type(tools, config):
    """read_file com start_line não-inteiro lança ValueError."""
    workspace = config.workspace_root
    (workspace / "test.txt").write_text("a\nb\nc\n", encoding="utf-8")

    call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": "abc"})
    with pytest.raises(ValueError):
        tools.read_file(call)


def test_read_file_staging_with_range(tools, config, tmp_path):
    """read_file com staging ativo e start_line/end_line lê intervalo do arquivo em staging."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "test.txt").write_text("x\ny\nz\nw\n", encoding="utf-8")

    with patch("quimera.runtime.tools.files.get_staging_root", return_value=staging):
        call = ToolCall(name="read_file", arguments={"path": "test.txt", "start_line": 2, "end_line": 3})
        result = tools.read_file(call)

    assert result.ok is True
    assert result.content == "y\nz\n"

def test_file_tools_grep_search_skips_noisy_dirs_by_default(tools, config):
    """grep_search ignora diretorios que poluem a edicao do workspace."""
    workspace = config.workspace_root
    (workspace / "app.py").write_text("marker_token\n", encoding="utf-8")
    cache_dir = workspace / ".venv" / "lib"
    cache_dir.mkdir(parents=True)
    (cache_dir / "noise.py").write_text("marker_token\n", encoding="utf-8")

    result = tools.grep_search(
        ToolCall(name="grep_search", arguments={"pattern": "marker_token"})
    )

    assert "app.py:1:marker_token" in result.content
    assert ".venv" not in result.content


def test_file_tools_grep_search_supports_include_glob(tools, config):
    """include_glob restringe resultados ao tipo de arquivo desejado."""
    workspace = config.workspace_root
    (workspace / "app.py").write_text("marker_token\n", encoding="utf-8")
    (workspace / "notes.md").write_text("marker_token\n", encoding="utf-8")

    result = tools.grep_search(
        ToolCall(
            name="grep_search",
            arguments={"pattern": "marker_token", "include_glob": "*.py"},
        )
    )

    assert "app.py:1:marker_token" in result.content
    assert "notes.md" not in result.content


def test_file_tools_grep_search_supports_max_results(tools, config):
    """max_results permite respostas menores para inspecao incremental."""
    workspace = config.workspace_root
    (workspace / "a.txt").write_text("marker_token\n", encoding="utf-8")
    (workspace / "b.txt").write_text("marker_token\n", encoding="utf-8")

    result = tools.grep_search(
        ToolCall(
            name="grep_search",
            arguments={"pattern": "marker_token", "max_results": 1},
        )
    )

    assert result.truncated is True
    assert len(result.content.splitlines()) == 1

