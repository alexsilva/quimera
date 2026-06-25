"""Tests for quimera/app/inputs.py"""
import queue
import sys
import threading
import pytest
from unittest.mock import MagicMock, patch, PropertyMock


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.renderer = MagicMock()
    app.system_layer = MagicMock()
    app._nonblocking_input_status = "idle"
    app._nonblocking_input_queue = None
    app._nonblocking_input_thread = None
    app._nonblocking_prompt_text = ""
    app._nonblocking_prompt_visible = True
    app._output_lock = None
    return app


@pytest.fixture
def resolver():
    return lambda: input


@pytest.fixture
def input_kwargs(mock_app):
    return {
        "set_input_status": lambda v: setattr(mock_app, "_nonblocking_input_status", v),
        "set_prompt_text": lambda v: setattr(mock_app, "_nonblocking_prompt_text", v),
        "set_prompt_owner": lambda v: setattr(mock_app, "_prompt_owning_thread_id", v),
        "set_prompt_visible": lambda v: setattr(mock_app, "_nonblocking_prompt_visible", v),
        "flush_deferred_messages": mock_app.system_layer.flush_deferred_messages,
    }


class TestAppInputServices:
    def test_init(self, mock_app, resolver):
        """Verifica que AppInputServices é inicializado corretamente."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(
            mock_app.renderer,
            resolver,
            get_input_status=lambda: mock_app._nonblocking_input_status,
            set_input_status=lambda v: setattr(mock_app, "_nonblocking_input_status", v),
            set_prompt_text=lambda v: setattr(mock_app, "_nonblocking_prompt_text", v),
            set_prompt_owner=lambda v: setattr(mock_app, "_prompt_owning_thread_id", v),
            set_prompt_visible=lambda v: setattr(mock_app, "_nonblocking_prompt_visible", v),
            flush_deferred_messages=mock_app.system_layer.flush_deferred_messages,
            output_lock=mock_app._output_lock,
        )
        assert srv.input_resolver is resolver
        assert srv._suspended is False

    def test_read_user_input_delegates(self, mock_app, resolver):
        """Verifica que read_user_input delega para a função de leitura."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(mock_app.renderer, resolver)
        with patch("quimera.app.inputs.read_user_input") as mock_read:
            mock_read.return_value = "hello"
            result = srv.read_user_input(">", 30)
            assert result == "hello"

    def test_read_from_editor_delegates(self, mock_app, resolver):
        """Verifica que read_from_editor delega para a função de editor."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(mock_app.renderer, resolver)
        with patch("quimera.app.inputs.read_from_editor") as mock_read:
            mock_read.return_value = "content"
            result = srv.read_from_editor()
            assert result == "content"

    def test_read_from_file_delegates(self, mock_app, resolver):
        """Verifica que read_from_file delega para a função de leitura de arquivo."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(mock_app.renderer, resolver)
        with patch("quimera.app.inputs.read_from_file") as mock_read:
            mock_read.return_value = "content"
            result = srv.read_from_file("/some/path")
            assert result == "content"

    def test_suspend_nonblocking_idle(self, mock_app, resolver):
        """Verifica que suspender quando idle não altera o estado."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(
            mock_app.renderer,
            resolver,
            get_input_status=lambda: mock_app._nonblocking_input_status,
            set_input_status=lambda v: setattr(mock_app, "_nonblocking_input_status", v),
            set_prompt_text=lambda v: setattr(mock_app, "_nonblocking_prompt_text", v),
            set_prompt_owner=lambda v: setattr(mock_app, "_prompt_owning_thread_id", v),
        )
        mock_app._nonblocking_input_status = "idle"
        srv.suspend_nonblocking()
        assert srv._suspended is False

    def test_suspend_nonblocking_reading(self, mock_app, resolver):
        """Verifica que suspender durante leitura pausa a entrada não-bloqueante."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(
            mock_app.renderer,
            resolver,
            get_input_status=lambda: mock_app._nonblocking_input_status,
            set_input_status=lambda v: setattr(mock_app, "_nonblocking_input_status", v),
            set_prompt_text=lambda v: setattr(mock_app, "_nonblocking_prompt_text", v),
            set_prompt_owner=lambda v: setattr(mock_app, "_prompt_owning_thread_id", v),
        )
        mock_app._nonblocking_input_status = "reading"
        with patch("sys.stdout") as mock_stdout:
            srv.suspend_nonblocking()
        assert srv._suspended is True
        assert mock_app._nonblocking_input_status == "idle"

    def test_resume_nonblocking_suspended(self, mock_app, resolver):
        """Verifica que retomar reativa a entrada não-bloqueante quando suspensa."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(
            mock_app.renderer,
            resolver,
            set_input_status=lambda v: setattr(mock_app, "_nonblocking_input_status", v),
            set_prompt_text=lambda v: setattr(mock_app, "_nonblocking_prompt_text", v),
            set_prompt_owner=lambda v: setattr(mock_app, "_prompt_owning_thread_id", v),
        )
        srv._suspended = True
        srv.resume_nonblocking()
        assert mock_app._nonblocking_input_status == "reading"
        assert srv._suspended is False

    def test_resume_nonblocking_not_suspended(self, mock_app, resolver):
        """Verifica que retomar quando não suspenso mantém estado idle."""
        from quimera.app.inputs import AppInputServices
        srv = AppInputServices(
            mock_app.renderer,
            resolver,
            set_input_status=lambda v: setattr(mock_app, "_nonblocking_input_status", v),
        )
        srv._suspended = False
        srv.resume_nonblocking()
        assert mock_app._nonblocking_input_status == "idle"


class TestReadUserInput:
    def test_timeout_positive_returns_value(self, mock_app, input_kwargs):
        """Verifica que read_user_input com timeout positivo retorna o valor lido."""
        from quimera.app.inputs import read_user_input
        with patch("quimera.app.inputs._tty.read_user_input_with_timeout") as mock_r:
            mock_r.return_value = "ok"
            result = read_user_input(mock_app.renderer, ">", 30, input_fn=MagicMock(), **input_kwargs)
            assert result == "ok"

    def test_timeout_positive_returns_none(self, mock_app, input_kwargs):
        """Verifica que read_user_input retorna None quando o timeout expira."""
        from quimera.app.inputs import read_user_input
        with patch("quimera.app.inputs._tty.read_user_input_with_timeout") as mock_r:
            mock_r.return_value = None
            result = read_user_input(mock_app.renderer, ">", 30, input_fn=MagicMock(), **input_kwargs)
            assert result is None
            mock_app.renderer.show_system.assert_called_once()

    def test_timeout_zero_stdin_none(self, mock_app, input_kwargs):
        """Verifica que read_user_input com timeout zero e sem stdin retorna None."""
        from quimera.app.inputs import read_user_input
        with patch("quimera.app.inputs._tty._stdin", return_value=None):
            result = read_user_input(mock_app.renderer, ">", 0, input_fn=MagicMock(), **input_kwargs)
            assert result is None

    def test_timeout_zero_tty_input(self, mock_app, input_kwargs):
        """Verifica que read_user_input lê do terminal quando timeout é zero."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        mock_input_fn = MagicMock(return_value="line")
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            result = read_user_input(mock_app.renderer, ">", 0, input_fn=mock_input_fn, **input_kwargs)
            assert result == "line"
            assert mock_app._nonblocking_input_status == "idle"
            mock_input_fn.assert_called_once_with(">")
            assert mock_app.system_layer.flush_deferred_messages.call_count == 2

    def test_timeout_zero_tty_keyboard_interrupt(self, mock_app, input_kwargs):
        """Verifica que read_user_input propaga KeyboardInterrupt no terminal."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        mock_input_fn = MagicMock(side_effect=KeyboardInterrupt)
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with pytest.raises(KeyboardInterrupt):
                read_user_input(mock_app.renderer, ">", 0, input_fn=mock_input_fn, **input_kwargs)
            assert mock_app._nonblocking_input_status == "idle"

    def test_timeout_zero_non_tty_select_ready(self, mock_app, input_kwargs):
        """Verifica que read_user_input lê de stdin não-TTY quando select indica dados prontos."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.readline.return_value = "line\n"
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", return_value=([True], [], [])):
                result = read_user_input(mock_app.renderer, ">", 0, input_fn=MagicMock(), **input_kwargs)
                assert result == "line"

    def test_timeout_zero_non_tty_select_not_ready(self, mock_app, input_kwargs):
        """Verifica que read_user_input retorna None quando select não tem dados."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", return_value=([], [], [])):
                with patch("time.sleep"):
                    result = read_user_input(mock_app.renderer, ">", 0, input_fn=MagicMock(), **input_kwargs)
                    assert result is None

    def test_timeout_zero_non_tty_select_exception(self, mock_app, input_kwargs):
        """Verifica que read_user_input trata exceção do select graciosamente."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", side_effect=Exception):
                result = read_user_input(mock_app.renderer, ">", 0, input_fn=MagicMock(), **input_kwargs)
                assert result is None

    def test_timeout_zero_non_tty_eof(self, mock_app, input_kwargs):
        """Verifica que read_user_input retorna None ao encontrar EOF."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.readline.return_value = ""
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", return_value=([True], [], [])):
                result = read_user_input(mock_app.renderer, ">", 0, input_fn=MagicMock(), **input_kwargs)
                assert result is None

    def test_timeout_none_eof(self, mock_app, input_kwargs):
        """Verifica que read_user_input propaga EOFError quando timeout é None."""
        from quimera.app.inputs import read_user_input
        input_fn = MagicMock(side_effect=EOFError)
        with pytest.raises(EOFError):
            read_user_input(mock_app.renderer, ">", None, input_fn=input_fn, **input_kwargs)

    def test_timeout_none_keyboard_interrupt(self, mock_app, input_kwargs):
        """Verifica que read_user_input propaga KeyboardInterrupt quando timeout é None."""
        from quimera.app.inputs import read_user_input
        input_fn = MagicMock(side_effect=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            read_user_input(mock_app.renderer, ">", None, input_fn=input_fn, **input_kwargs)
        assert mock_app._nonblocking_prompt_visible is False

    def test_timeout_none_normal(self, mock_app, input_kwargs):
        """Verifica que read_user_input retorna valor normalmente quando timeout é None."""
        from quimera.app.inputs import read_user_input
        input_fn = MagicMock(return_value="hello")
        result = read_user_input(mock_app.renderer, ">", None, input_fn=input_fn, **input_kwargs)
        assert result == "hello"
        assert mock_app._nonblocking_prompt_visible is False

    def test_timeout_zero_stdin_isatty_exception(self, mock_app, input_kwargs):
        """Verifica que read_user_input trata exceção em isatty graciosamente."""
        from quimera.app.inputs import read_user_input
        mock_stdin = MagicMock()
        mock_stdin.isatty.side_effect = RuntimeError("boom")
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            result = read_user_input(mock_app.renderer, ">", 0, input_fn=MagicMock(), **input_kwargs)
            assert result is None


class TestReadUserInputWithTimeout:
    def test_non_tty_ready(self, mock_app):
        """Verifica que read_user_input_with_timeout lê de stdin não-TTY quando pronto."""
        from quimera.app.inputs import read_user_input_with_timeout
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.readline.return_value = "line\n"
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", return_value=([True], [], [])):
                result = read_user_input_with_timeout(">", 5, input_fn=MagicMock())
                assert result == "line"

    def test_non_tty_not_ready(self, mock_app):
        """Verifica que read_user_input_with_timeout retorna None quando stdin não está pronto."""
        from quimera.app.inputs import read_user_input_with_timeout
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", return_value=([], [], [])):
                result = read_user_input_with_timeout(">", 5, input_fn=MagicMock())
                assert result is None

    def test_non_tty_eof(self, mock_app):
        """Verifica que read_user_input_with_timeout retorna None ao encontrar EOF."""
        from quimera.app.inputs import read_user_input_with_timeout
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.readline.return_value = ""
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", return_value=([True], [], [])):
                result = read_user_input_with_timeout(">", 5, input_fn=MagicMock())
                assert result is None

    def test_non_tty_select_exception(self, mock_app):
        """Verifica que read_user_input_with_timeout trata exceção do select."""
        from quimera.app.inputs import read_user_input_with_timeout
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("select.select", side_effect=Exception):
                result = read_user_input_with_timeout(">", 5, input_fn=MagicMock())
                assert result is None
                mock_stdin.readline.assert_not_called()

    def test_tty_returns_value(self, mock_app):
        """Verifica que read_user_input_with_timeout lê do terminal TTY."""
        from quimera.app.inputs import read_user_input_with_timeout
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            result = read_user_input_with_timeout(">", 5, input_fn=lambda p: "hello")
            assert result == "hello"

    def test_tty_queue_timeout(self, mock_app):
        """Verifica que read_user_input_with_timeout retorna None quando a fila está vazia."""
        from quimera.app.inputs import read_user_input_with_timeout
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        with patch("quimera.app.inputs._tty._stdin", return_value=mock_stdin):
            with patch("queue.Queue.get", side_effect=queue.Empty):
                result = read_user_input_with_timeout(">", 5, input_fn=MagicMock())
                assert result is None


class TestReadFromEditor:
    def test_no_editor_found(self, mock_app):
        """Verifica que read_from_editor retorna None quando nenhum editor é encontrado."""
        from quimera.app.inputs import read_from_editor
        with patch("shutil.which", return_value=None):
            with patch.dict("os.environ", {}, clear=True):
                result = read_from_editor(mock_app.renderer)
                assert result is None
                mock_app.renderer.show_error.assert_called_once()

    def test_editor_from_env_success(self, mock_app):
        """Verifica que read_from_editor usa o editor definido em EDITOR com sucesso."""
        from quimera.app.inputs import read_from_editor
        with patch("shutil.which", return_value="/usr/bin/nano"):
            with patch.dict("os.environ", {"EDITOR": "nano"}, clear=True):
                with patch("subprocess.run"):
                    with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                        mock_tmp.return_value.__enter__.return_value.name = "/tmp/test.md"
                        with patch("pathlib.Path.read_text", return_value="content"):
                            result = read_from_editor(mock_app.renderer)
                            assert result == "content"

    def test_called_process_error(self, mock_app):
        """Verifica que read_from_editor trata CalledProcessError graciosamente."""
        from quimera.app.inputs import read_from_editor
        import subprocess
        with patch("shutil.which", return_value="/usr/bin/nano"):
            with patch.dict("os.environ", {"EDITOR": "nano"}, clear=True):
                with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "nano")):
                    with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                        mock_tmp.return_value.__enter__.return_value.name = "/tmp/test.md"
                        with patch("pathlib.Path.read_text", return_value="content"):
                            result = read_from_editor(mock_app.renderer)
                            assert result is None
                            mock_app.renderer.show_error.assert_called_once()

    def test_with_output_lock(self, mock_app):
        """Verifica que read_from_editor funciona com output_lock."""
        from quimera.app.inputs import read_from_editor
        lock = threading.Lock()
        mock_app._output_lock = lock
        with patch("shutil.which", return_value="/usr/bin/nano"):
            with patch.dict("os.environ", {"EDITOR": "nano"}, clear=True):
                with patch("subprocess.run"):
                    with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                        mock_tmp.return_value.__enter__.return_value.name = "/tmp/test.md"
                        with patch("pathlib.Path.read_text", return_value="content"):
                            result = read_from_editor(mock_app.renderer, output_lock=lock)
                            assert result == "content"

    def test_empty_content_returns_none(self, mock_app):
        """Verifica que read_from_editor retorna None quando o conteúdo é vazio."""
        from quimera.app.inputs import read_from_editor
        with patch("shutil.which", return_value="/usr/bin/nano"):
            with patch.dict("os.environ", {"EDITOR": "nano"}, clear=True):
                with patch("subprocess.run"):
                    with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                        mock_tmp.return_value.__enter__.return_value.name = "/tmp/test.md"
                        with patch("pathlib.Path.read_text", return_value="  \n  \n"):
                            result = read_from_editor(mock_app.renderer)
                            assert result is None


class TestReadFromFile:
    def test_file_not_found(self, mock_app):
        """Verifica que read_from_file retorna None quando o arquivo não existe."""
        from quimera.app.inputs import read_from_file
        with patch("pathlib.Path.exists", return_value=False):
            result = read_from_file(mock_app.renderer, "/nonexistent")
            assert result is None
            mock_app.renderer.show_error.assert_called_once()

    def test_file_found(self, mock_app):
        """Verifica que read_from_file retorna o conteúdo do arquivo."""
        from quimera.app.inputs import read_from_file
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="hello\n"):
                result = read_from_file(mock_app.renderer, "/some/path")
                assert result == "hello"

    def test_file_found_strips_surrounding_whitespace_from_path(self, mock_app, tmp_path):
        """/file README.md deve resolver README.md, sem preservar espaço inicial."""
        from quimera.app.inputs import read_from_file

        target = tmp_path / "README.md"
        target.write_text("hello\n", encoding="utf-8")

        result = read_from_file(mock_app.renderer, f"  {target}  ")

        assert result == "hello"
        mock_app.renderer.show_error.assert_not_called()

    def test_file_found_empty_content(self, mock_app):
        """Verifica que read_from_file retorna None quando o conteúdo é apenas espaços."""
        from quimera.app.inputs import read_from_file
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="  \n  \n"):
                result = read_from_file(mock_app.renderer, "/some/path")
                assert result is None


class TestNormalizeLoadedContent:
    def test_normalize_crlf(self):
        """Verifica que _normalize_loaded_content converte CRLF para LF."""
        from quimera.app.inputs import _normalize_loaded_content
        assert _normalize_loaded_content("a\r\nb\r\n") == "a\nb"

    def test_normalize_cr(self):
        """Verifica que _normalize_loaded_content converte CR para LF."""
        from quimera.app.inputs import _normalize_loaded_content
        assert _normalize_loaded_content("a\rb\r") == "a\nb"

    def test_only_whitespace(self):
        """Verifica que _normalize_loaded_content retorna None para conteúdo só com espaços."""
        from quimera.app.inputs import _normalize_loaded_content
        assert _normalize_loaded_content("   \n  ") is None

    def test_none_input(self):
        """Verifica que _normalize_loaded_content retorna None para entrada None."""
        from quimera.app.inputs import _normalize_loaded_content
        assert _normalize_loaded_content(None) is None

    def test_valid_content(self):
        """Verifica que _normalize_loaded_content retorna conteúdo válido normalizado."""
        from quimera.app.inputs import _normalize_loaded_content
        assert _normalize_loaded_content("  hello  \n") == "  hello  "

    def test_empty_string(self):
        """Verifica que _normalize_loaded_content retorna None para string vazia."""
        from quimera.app.inputs import _normalize_loaded_content
        assert _normalize_loaded_content("") is None


class TestStdin:
    def test_stdin_returns_sys_stdin(self):
        """Verifica que _stdin retorna sys.stdin."""
        from quimera.app.inputs import _stdin
        assert _stdin() is sys.stdin
