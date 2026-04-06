import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import subprocess
from quimera.context import ContextManager

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
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    assert cm.load_base() == "Base Content"

def test_load_base_not_exists(tmp_path, renderer):
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "session.md"
    cm = ContextManager(base, session, renderer)
    assert cm.load_base() == ""

def test_load_session(temp_files, renderer):
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    assert cm.load_session() == "## Resumo da última sessão\n\n_Gerado em 2026-01-01 10:00_\n\nActual Summary"

def test_load_session_summary(temp_files, renderer):
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    assert cm.load_session_summary() == "Actual Summary"

def test_load_session_summary_invalid(tmp_path, renderer):
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    session.write_text("Invalid Summary", encoding="utf-8")
    cm = ContextManager(base, session, renderer)
    assert cm.load_session_summary() == ""

def test_load_session_summary_empty(tmp_path, renderer):
    base = tmp_path / "base.md"
    session = tmp_path / "session.md"
    session.write_text("## Resumo da última sessão\n\n", encoding="utf-8")
    cm = ContextManager(base, session, renderer)
    assert cm.load_session_summary() == ""

def test_load_combined(temp_files, renderer):
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    combined = cm.load()
    assert "Base Content" in combined
    assert "## Resumo da última sessão" in combined

def test_load_only_base(tmp_path, renderer):
    base = tmp_path / "base.md"
    base.write_text("Base Only", encoding="utf-8")
    session = tmp_path / "nonexistent.md"
    cm = ContextManager(base, session, renderer)
    assert cm.load() == "Base Only"

def test_load_only_session(tmp_path, renderer):
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "session.md"
    session.write_text("Session Only", encoding="utf-8")
    cm = ContextManager(base, session, renderer)
    assert cm.load() == "Session Only"

def test_load_empty(tmp_path, renderer):
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "nonexistent.md"
    cm = ContextManager(base, session, renderer)
    assert cm.load() == ""

def test_show(temp_files, renderer):
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.show()
    renderer.show_plain.assert_called_once()

def test_show_empty(tmp_path, renderer):
    base = tmp_path / "nonexistent.md"
    session = tmp_path / "nonexistent.md"
    cm = ContextManager(base, session, renderer)
    cm.show()
    renderer.show_system.assert_called_with("\n[contexto vazio]\n")

@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_with_editor_env(mock_run, mock_get, temp_files, renderer):
    mock_get.return_value = "code --wait"
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    mock_run.assert_called_once_with(["code", "--wait", str(base)], check=True)

@patch('os.environ.get')
@patch('shutil.which')
@patch('subprocess.run')
def test_edit_fallback_editor(mock_run, mock_which, mock_get, temp_files, renderer):
    mock_get.return_value = None
    mock_which.side_effect = lambda x: x == "nano"
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    mock_run.assert_called_once_with(["nano", str(base)], check=True)

@patch('os.environ.get')
@patch('shutil.which')
def test_edit_no_editor_found(mock_which, mock_get, temp_files, renderer):
    mock_get.return_value = None
    mock_which.return_value = None
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    renderer.show_error.assert_called_with("\nNenhum editor disponível. Instale nano, vim ou vi.\n")

@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_file_not_found(mock_run, mock_get, temp_files, renderer):
    mock_get.return_value = "nonexistent_editor"
    mock_run.side_effect = FileNotFoundError
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    renderer.show_error.assert_called()

@patch('os.environ.get')
@patch('subprocess.run')
def test_edit_error(mock_run, mock_get, temp_files, renderer):
    mock_get.return_value = "vim"
    mock_run.side_effect = subprocess.CalledProcessError(1, "vim")
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.edit()
    renderer.show_error.assert_called()

def test_update_with_summary(temp_files, renderer):
    base, session = temp_files
    cm = ContextManager(base, session, renderer)
    cm.update_with_summary("New Summary")
    content = session.read_text(encoding="utf-8")
    assert "## Resumo da última sessão" in content
    assert "New Summary" in content
    renderer.show_system.assert_called()
