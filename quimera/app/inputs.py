"""Componentes de `quimera.app.inputs`."""
import os
import queue
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class AppInputServices:
    """Agrupa operações de entrada e edição usadas pela aplicação."""

    def __init__(self, app, input_resolver):
        """Inicializa uma instância de AppInputServices."""
        self.app = app
        self.input_resolver = input_resolver
        self._suspended = False

    def read_user_input(self, prompt, timeout: int) -> str | None:
        """Lê entrada do usuário com a política de timeout configurada."""
        return read_user_input(self.app, prompt, timeout, input_fn=self.input_resolver())

    def read_from_editor(self):
        """Abre o editor configurado e retorna o conteúdo digitado."""
        return read_from_editor(self.app)

    def read_from_file(self, path_str):
        """Lê conteúdo de um arquivo fornecido pelo usuário."""
        return read_from_file(self.app, path_str)

    def suspend_nonblocking(self):
        """Pausa o estado não-bloqueante para permitir input bloqueante limpo."""
        app = self.app
        was_reading = app._nonblocking_input_status == "reading"
        self._suspended = was_reading
        if was_reading:
            app._nonblocking_input_status = "idle"
            # Limpa qualquer resíduo visual da linha de prompt
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()

    def resume_nonblocking(self):
        """Restaura o estado não-bloqueante após input bloqueante."""
        if self._suspended:
            self.app._nonblocking_input_status = "reading"
            self._suspended = False


def read_user_input(app, prompt, timeout: int, input_fn=input) -> str | None:
    """Lê user input."""
    if timeout and timeout > 0:
        value = read_user_input_with_timeout(prompt, timeout, input_fn=input_fn)
        if value is None:
            app.renderer.show_system(f"*idle* ({timeout}s sem activity)")
            return None
        return value

    if timeout == 0:
        try:
            stdin = _stdin()
            if stdin is None:
                return None
            if stdin.isatty():
                app._nonblocking_input_status = "reading"
                app._nonblocking_prompt_text = prompt
                app.system_layer.flush_deferred_messages()
                try:
                    return input_fn(prompt)
                finally:
                    app._nonblocking_input_status = "idle"
                    app._nonblocking_prompt_text = ""
            if select.select([stdin], [], [], 0)[0]:
                line = stdin.readline()
                if line == "":
                    return None
                return line.rstrip("\r\n")
            time.sleep(0.01)
            return None
        except Exception:
            return None

    try:
        app._nonblocking_prompt_visible = False
        return input_fn(prompt)
    except EOFError:
        if timeout == 0:
            return None
        raise
    except KeyboardInterrupt:
        app._nonblocking_prompt_visible = False
        print()
        raise


def read_user_input_nonblocking_tty(app, prompt: str, input_fn=input) -> str | None:
    """Lê user input nonblocking tty."""
    if app._nonblocking_input_queue is None:
        app._nonblocking_input_queue = queue.Queue()

    try:
        status, value = app._nonblocking_input_queue.get_nowait()
    except queue.Empty:
        thread = app._nonblocking_input_thread
        if thread is None or not thread.is_alive():
            start_nonblocking_input_reader(app, prompt, input_fn=input_fn)
        else:
            # Amortece o polling sem adicionar atraso perceptível ao retorno do prompt.
            time.sleep(0.01)
        return None

    app._nonblocking_input_status = "idle"
    app._nonblocking_input_thread = None
    app._nonblocking_prompt_text = ""
    app.system_layer.flush_deferred_messages()
    if status == "line":
        return value
    if status == "interrupt":
        raise KeyboardInterrupt()
    return None


def start_nonblocking_input_reader(app, prompt: str, input_fn=input) -> None:
    """Executa start nonblocking input reader."""
    if app._nonblocking_input_queue is None:
        app._nonblocking_input_queue = queue.Queue()

    app._nonblocking_input_status = "reading"
    app._nonblocking_prompt_text = prompt

    def _reader() -> None:
        try:
            value = input_fn(prompt)
        except EOFError:
            app._nonblocking_input_queue.put(("eof", None))
        except KeyboardInterrupt:
            app._nonblocking_input_queue.put(("interrupt", None))
        except Exception:
            app._nonblocking_input_queue.put(("error", None))
        else:
            app._nonblocking_input_queue.put(("line", value))

    app._nonblocking_input_thread = threading.Thread(target=_reader, daemon=True)
    app._nonblocking_input_thread.start()


def read_user_input_with_timeout(prompt: str, timeout: int, input_fn=input):
    """Lê user input with timeout."""
    stdin = _stdin()
    if stdin is not None and not stdin.isatty():
        try:
            ready, _, _ = select.select([stdin], [], [], timeout)
        except Exception:
            return None
        if not ready:
            return None
        line = stdin.readline()
        if line == "":
            return None
        return line.rstrip("\r\n")

    result_queue = queue.Queue()

    def _reader():
        try:
            result_queue.put(input_fn(prompt))
        except Exception:
            result_queue.put(None)

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    try:
        return result_queue.get(timeout=timeout)
    except queue.Empty:
        return None


def read_from_editor(app):
    """Lê from editor."""
    editor_env = os.environ.get("EDITOR", "")
    if editor_env:
        editor_parts = shlex.split(editor_env)
    else:
        fallbacks = ["nano", "vim", "vi"]
        editor_parts = next(([editor] for editor in fallbacks if shutil.which(editor)), None)
        if not editor_parts:
            app.renderer.show_error("\nNenhum editor encontrado. Defina $EDITOR ou instale nano/vim.\n")
            return None

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run([*editor_parts, tmp_path], check=True)
        content = Path(tmp_path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        app.renderer.show_error(f"\nEditor não encontrado: {editor_parts[0]}\n")
        return None
    except subprocess.CalledProcessError as exc:
        app.renderer.show_error(f"\nEditor encerrou com erro (código {exc.returncode}).\n")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return content or None


def read_from_file(app, path_str):
    """Lê from file."""
    path = Path(path_str).expanduser()
    if not path.exists():
        app.renderer.show_error(f"\nArquivo não encontrado: {path}\n")
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def _stdin():
    """Executa stdin."""
    return sys.stdin
