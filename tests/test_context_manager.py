import subprocess
from unittest.mock import MagicMock, patch

import pytest

from quimera.context import ContextManager
from quimera.workspace import Workspace


@pytest.fixture
def temp_files(tmp_path):
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    base.write_text("Base Content", encoding="utf-8")
    session.write_text("## Resumo da última sessão\n\n_Gerado em 2026-01-01 10:00_\n\nActual Summary", encoding="utf-8")
    return base, session


@pytest.fixture
def renderer():
    return MagicMock()


def test_load_base(temp_files, renderer):
    """Verifica que load_base retorna o conteúdo do arquivo base."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    assert cm.load_base() == "Base Content"


def test_load_base_not_exists(tmp_path, renderer):
    """Verifica que load_base retorna vazio quando o arquivo base não existe."""
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "session.md"
    cm = ContextManager(base, session, renderer)
    assert cm.load_base() == ""


def test_load_session(temp_files, renderer):
    """Verifica que load_session retorna o conteúdo completo do arquivo de sessão."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    assert cm.load_session() == "## Resumo da última sessão\n\n_Gerado em 2026-01-01 10:00_\n\nActual Summary"


def test_load_session_summary(temp_files, renderer):
    """Verifica que load_session_summary extrai o resumo do arquivo de sessão."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    assert cm.load_session_summary() == "Actual Summary"


def test_load_session_summary_invalid(tmp_path, renderer):
    """Verifica que resumo inválido retorna string vazia."""
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    session.write_text("Invalid Summary", encoding="utf-8")
    cm = ContextManager(base, session, renderer)
    assert cm.load_session_summary() == ""


def test_load_session_summary_empty(tmp_path, renderer):
    """Verifica que resumo vazio retorna string vazia."""
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    session.write_text("## Resumo da última sessão\n\n", encoding="utf-8")
    cm = ContextManager(base, session, renderer)
    assert cm.load_session_summary() == ""


def test_load_combined(temp_files, renderer):
    """Verifica que load combina base e sessão, omitindo o cabeçalho do resumo."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    combined = cm.load()
    assert "Base Content" in combined
    assert "Actual Summary" in combined
    assert "## Resumo da última sessão" not in combined


def test_load_only_base(tmp_path, renderer):
    """Verifica que load retorna apenas o base quando não há sessão."""
    base = tmp_path / "base.md"
    base.write_text("Base Only", encoding="utf-8")
    session = tmp_path / "nonexistent.md"
    cm = ContextManager(base, session, renderer)
    assert cm.load() == "Base Only"


def test_load_only_session(tmp_path, renderer):
    """Verifica que load retorna apenas a sessão quando não há base."""
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "session.md"
    session.write_text("Session Only", encoding="utf-8")
    cm = ContextManager(base, session, renderer)
    assert cm.load() == "Session Only"


def test_load_empty(tmp_path, renderer):
    """Verifica que load retorna vazio quando não há base nem sessão."""
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "nonexistent.md"
    cm = ContextManager(base, session, renderer)
    assert cm.load() == ""


def test_show(temp_files, renderer):
    """Verifica que show exibe o conteúdo base através do renderer."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.show()
    renderer.show_plain.assert_called_once()


def test_show_empty(tmp_path, renderer):
    """Verifica que show exibe mensagem de contexto vazio quando não há conteúdo."""
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "nonexistent.md"
    cm = ContextManager(base, session, renderer)
    cm.show()
    renderer.show_system.assert_called_with("\n[contexto vazio]\n")


@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_with_editor_env(mock_run, mock_get, temp_files, renderer):
    """Verifica que edit usa o editor definido em EDITOR."""

    mock_get.return_value = "code --wait"
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    mock_run.assert_called_once_with(["code", "--wait", str(base)], check=True)
    renderer.external_window.assert_called_once()


@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_uses_external_editor_window(mock_run, mock_get, temp_files, renderer):
    """Verifica que edit usa uma janela externa para posse do terminal."""

    mock_get.return_value = "code --wait"
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    mock_run.assert_called_once_with(["code", "--wait", str(base)], check=True)
    renderer.external_window.assert_called_once()


@patch('os.environ.get')
@patch('shutil.which')
@patch('subprocess.run')
def test_edit_fallback_editor(mock_run, mock_which, mock_get, temp_files, renderer):
    """Verifica que edit usa nano como fallback quando EDITOR não está definido."""

    mock_get.return_value = None
    mock_which.side_effect = lambda x: x == "nano"
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    mock_run.assert_called_once_with(["nano", str(base)], check=True)


@patch('os.environ.get')
@patch('shutil.which')
def test_edit_no_editor_found(mock_which, mock_get, temp_files, renderer):
    """Verifica que edit exibe erro quando nenhum editor é encontrado."""

    mock_get.return_value = None
    mock_which.return_value = None
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    renderer.show_error.assert_called_with("\nNenhum editor encontrado. Defina $EDITOR ou instale nano/vim.\n")


@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_file_not_found(mock_run, mock_get, temp_files, renderer):
    """Verifica que edit lida com FileNotFoundError do editor."""

    mock_get.return_value = "nonexistent_editor"
    mock_run.side_effect = FileNotFoundError
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    renderer.show_error.assert_called()


@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_error(mock_run, mock_get, temp_files, renderer):
    """Verifica que edit exibe erro quando o subprocesso falha."""

    mock_get.return_value = "vim"
    mock_run.side_effect = subprocess.CalledProcessError(1, "vim")
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    renderer.show_error.assert_called()


def test_update_with_summary(temp_files, renderer):
    """Verifica que update_with_summary salva resumo sem publicar no chat."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.update_with_summary("New Summary")
    content = session.read_text(encoding="utf-8")
    assert "## Resumo da última sessão" in content
    assert "New Summary" in content
    renderer.show_system.assert_not_called()
    renderer.show_notification.assert_called_once_with("Resumo salvo em session.md")


def test_load_previous_session_exists(tmp_path, renderer):
    """Verifica que load_previous_session retorna o conteúdo quando o arquivo existe."""
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    previous = tmp_path / "previous_session.md"
    previous.write_text("Previous session content", encoding="utf-8")
    cm = ContextManager(base, session, renderer, previous_session_file=previous)
    assert cm.load_previous_session() == "Previous session content"


def test_load_previous_session_not_exists(temp_files, renderer):
    """Verifica que load_previous_session retorna vazio quando não há arquivo anterior."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer, previous_session_file=None)
    assert cm.load_previous_session() == ""


def test_load_with_previous_session(tmp_path, renderer):
    """Verifica que load combina base e sessão sem incluir a sessão anterior."""
    base = tmp_path / "base.md"
    base.write_text("Base Context", encoding="utf-8")
    session = tmp_path / "session.md"
    session.write_text("Current Session", encoding="utf-8")
    previous = tmp_path / "previous_session.md"
    previous.write_text("Previous Summary", encoding="utf-8")
    cm = ContextManager(base, session, renderer, previous_session_file=previous)
    result = cm.load()
    assert "Base Context" in result
    assert "Current Session" in result
    base_idx = result.index("Base Context")
    sess_idx = result.index("Current Session")
    assert base_idx < sess_idx
    assert "Previous Summary" not in result


def test_load_without_previous_session(temp_files, renderer):
    """Verifica que load funciona normalmente sem arquivo de sessão anterior."""
    base, session = temp_files
    cm = ContextManager(base, session, renderer, previous_session_file=None)
    result = cm.load()
    assert "Base Content" in result
    assert "Actual Summary" in result
    assert "## Resumo da última sessão" not in result
    assert "Previous" not in result


def test_load_filters_pending_sections_from_session_summary(tmp_path, renderer):
    """Verifica que load filtra seções de pendências do resumo da sessão."""
    base = tmp_path / "base.md"
    base.write_text("Base Context", encoding="utf-8")
    session = tmp_path / "session.md"
    session.write_text(
        "## Resumo da última sessão\n\n"
        "_Gerado em 2026-01-01 10:00_\n\n"
        "## Decisões tomadas\n"
        "- manter filtro\n\n"
        "## Pendências ou próximos passos\n"
        "- corrigir objetivo antigo\n",
        encoding="utf-8",
    )

    cm = ContextManager(base, session, renderer)

    result = cm.load()

    assert "Base Context" in result
    assert "Decisões tomadas" in result
    assert "manter filtro" in result
    assert "Pendências ou próximos passos" not in result
    assert "corrigir objetivo antigo" not in result


def test_save_previous_session(tmp_path, renderer):
    """Verifica que save_previous_session persiste o resumo no arquivo anterior."""
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    previous = tmp_path / "previous_session.md"
    cm = ContextManager(base, session, renderer, previous_session_file=previous)
    cm.save_previous_session("Test summary")
    content = previous.read_text(encoding="utf-8")
    assert "Test summary" in content


def test_save_previous_session_without_file_is_noop(tmp_path, renderer):
    """Verifica que save_previous_session não faz nada quando não há arquivo configurado."""
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    cm = ContextManager(base, session, renderer, previous_session_file=None)
    cm.save_previous_session("Ignored summary")
    assert not (tmp_path / "previous_session.md").exists()


def test_load_truncates_context_to_max_lines(tmp_path, renderer):
    """Verifica que load trunca o contexto respeitando max_context_lines."""
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    base.write_text("linha 1\nlinha 2", encoding="utf-8")
    session.write_text("linha 3\nlinha 4", encoding="utf-8")
    cm = ContextManager(base, session, renderer, max_context_lines=3)

    result = cm.load()

    assert result == "\nlinha 3\nlinha 4"


@patch("os.environ.get")
@patch("subprocess.run")
def test_context_branch_switch_updates_edit_and_load_base(mock_run, mock_get, tmp_path, renderer):
    """Verifica que a troca de branch atualiza o contexto e o editor."""

    mock_get.return_value = "code --wait"
    base_dir = tmp_path / "base"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    with patch("quimera.workspace.find_base_writable", lambda dirs: base_dir):
        workspace = Workspace(project_dir)
        workspace.set_branch("feature/anterior")
        old_context_path = workspace.context_persistent
        old_context_path.write_text("conteudo antigo", encoding="utf-8")

        cm = ContextManager(
            old_context_path,
            workspace.context_session,
            renderer,
            workspace=workspace,
        )

        assert cm.handle_context_branch("/context-branch feature/PC-12073") is True
        new_context_path = workspace.context_persistent
        new_context_path.write_text("conteudo novo", encoding="utf-8")

        assert cm.load_base() == "conteudo novo"

        cm.edit()
        mock_run.assert_called_once_with(["code", "--wait", str(new_context_path)], check=True)
