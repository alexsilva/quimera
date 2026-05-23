"""Componentes de `quimera.app.inputs`."""
from __future__ import annotations

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
from contextlib import nullcontext
from pathlib import Path

from .interfaces import IRenderer


class AppInputServices:
    """Agrupa operações de entrada e edição usadas pela aplicação."""

    def __init__(
        self,
        renderer: IRenderer,
        input_resolver,
        *,
        get_input_status=None,
        set_input_status=None,
        set_prompt_owner=None,
        set_prompt_text=None,
        set_prompt_visible=None,
        flush_deferred_messages=None,
        output_lock=None,
    ):
        """Inicializa uma instância de AppInputServices."""
        self._renderer = renderer
        self.input_resolver = input_resolver
        self._get_input_status = get_input_status or (lambda: "idle")
        self._set_input_status = set_input_status or (lambda _v: None)
        self._set_prompt_owner = set_prompt_owner or (lambda _v: None)
        self._set_prompt_text = set_prompt_text or (lambda _v: None)
        self._set_prompt_visible = set_prompt_visible or (lambda _v: None)
        self._flush_deferred_messages = flush_deferred_messages or (lambda: None)
        self._output_lock = output_lock
        self._suspended = False
        self._nonblocking_tty = False
        self._input_queue: queue.Queue | None = None
        self._input_thread: threading.Thread | None = None

    def set_nonblocking_tty(self, enabled: bool) -> None:
        """Ativa/desativa leitura não-bloqueante de TTY via thread de background."""
        self._nonblocking_tty = enabled

    def read_user_input(self, prompt, timeout: int) -> str | None:
        """Lê entrada do usuário com a política de timeout configurada."""
        if timeout == 0 and self._nonblocking_tty:
            stdin = _stdin()
            if stdin is not None and stdin.isatty():
                return self._read_nonblocking_tty(prompt)
        # Usa input_resolver() para obter o callable de input atual (InputGate ou input).
        return read_user_input(
            self._renderer,
            prompt,
            timeout,
            input_fn=self.input_resolver(),
            set_input_status=self._set_input_status,
            set_prompt_text=self._set_prompt_text,
            set_prompt_owner=self._set_prompt_owner,
            set_prompt_visible=self._set_prompt_visible,
            flush_deferred_messages=self._flush_deferred_messages,
        )

    def _read_nonblocking_tty(self, prompt: str) -> str | None:
        """Lê input de TTY de forma não-bloqueante usando thread de background."""
        if self._input_queue is None:
            self._input_queue = queue.Queue()
        try:
            status, value = self._input_queue.get_nowait()
        except queue.Empty:
            if self._input_thread is None or not self._input_thread.is_alive():
                self._start_input_reader(prompt)
            else:
                time.sleep(0.01)
            return None
        self._input_thread = None
        self._flush_deferred_messages()
        if status == "line":
            return value
        if status == "interrupt":
            raise KeyboardInterrupt()
        return None

    def _start_input_reader(self, prompt: str) -> None:
        """Inicia thread de background para ler input de TTY sem bloquear."""
        if self._input_queue is None:
            self._input_queue = queue.Queue()
        input_fn = self.input_resolver()

        def _reader() -> None:
            self._set_input_status("reading")
            self._set_prompt_text(prompt)
            self._set_prompt_owner(threading.get_ident())
            try:
                value = input_fn(prompt)
            except EOFError:
                self._input_queue.put(("eof", None))
            except KeyboardInterrupt:
                self._input_queue.put(("interrupt", None))
            except Exception:
                self._input_queue.put(("error", None))
            else:
                self._input_queue.put(("line", value))
            finally:
                self._set_input_status("idle")
                self._set_prompt_text("")
                self._set_prompt_owner(None)

        self._input_thread = threading.Thread(target=_reader, daemon=True)
        self._input_thread.start()

    def read_from_editor(self):
        """Abre o editor configurado e retorna o conteúdo digitado."""
        return read_from_editor(self._renderer, output_lock=self._output_lock)

    def read_from_file(self, path_str):
        """Lê conteúdo de um arquivo fornecido pelo usuário."""
        return read_from_file(self._renderer, path_str)

    def suspend_nonblocking(self):
        """Pausa o estado não-bloqueante para permitir input bloqueante limpo."""
        was_reading = self._get_input_status() == "reading"
        self._suspended = was_reading
        if was_reading:
            self._set_input_status("idle")
            self._set_prompt_text("")
            self._set_prompt_owner(None)

    def resume_nonblocking(self):
        """Restaura o estado não-bloqueante após input bloqueante."""
        if self._suspended:
            self._set_input_status("reading")
            self._set_prompt_text("")
            self._set_prompt_owner(threading.get_ident())
            self._suspended = False


def read_user_input(
    renderer: IRenderer,
    prompt,
    timeout: int,
    *,
    input_fn=input,
    set_input_status=lambda _v: None,
    set_prompt_text=lambda _v: None,
    set_prompt_owner=lambda _v: None,
    set_prompt_visible=lambda _v: None,
    flush_deferred_messages=lambda: None,
) -> str | None:
    """Lê user input."""
    if timeout and timeout > 0:
        value = read_user_input_with_timeout(prompt, timeout, input_fn=input_fn)
        if value is None:
            renderer.show_system(f"*idle* ({timeout}s sem activity)")
            return None
        return value

    if timeout == 0:
        try:
            stdin = _stdin()
            if stdin is None:
                return None
            if stdin.isatty():
                set_input_status("reading")
                set_prompt_text(prompt)
                set_prompt_owner(threading.get_ident())
                flush_deferred_messages()
                try:
                    return input_fn(prompt)
                except KeyboardInterrupt:
                    print()
                    raise
                finally:
                    set_input_status("idle")
                    set_prompt_text("")
                    set_prompt_owner(None)
                    flush_deferred_messages()
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
        set_prompt_visible(False)
        return input_fn(prompt)
    except EOFError:
        if timeout == 0:
            return None
        raise
    except KeyboardInterrupt:
        set_prompt_visible(False)
        print()
        raise


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


def read_from_editor(renderer: IRenderer, output_lock=None):
    """Lê from editor."""
    editor_env = os.environ.get("EDITOR", "")
    if editor_env:
        editor_parts = shlex.split(editor_env)
    else:
        fallbacks = ["nano", "vim", "vi"]
        editor_parts = next(([editor] for editor in fallbacks if shutil.which(editor)), None)
        if not editor_parts:
            renderer.show_error("\nNenhum editor encontrado. Defina $EDITOR ou instale nano/vim.\n")
            return None

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
        tmp_path = tmp.name

    with (output_lock if output_lock is not None else nullcontext()):
        try:
            subprocess.run([*editor_parts, tmp_path], check=True)
            sys.stdout.write("\n")
            sys.stdout.flush()
            content = Path(tmp_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            renderer.show_error(f"\nEditor não encontrado: {editor_parts[0]}\n")
            return None
        except subprocess.CalledProcessError as exc:
            renderer.show_error(f"\nEditor encerrou com erro (código {exc.returncode}).\n")
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    return _normalize_loaded_content(content)


def read_from_file(renderer: IRenderer, path_str):
    """Lê from file."""
    path = Path(path_str).expanduser()
    if not path.exists():
        renderer.show_error(f"\nArquivo não encontrado: {path}\n")
        return None
    content = path.read_text(encoding="utf-8")
    return _normalize_loaded_content(content)


def _normalize_loaded_content(content: str) -> str | None:
    """Normaliza conteúdo de /edit e /file sem perder linhas úteis."""
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not normalized.strip():
        return None
    return normalized


def _stdin():
    """Executa stdin."""
    return sys.stdin
