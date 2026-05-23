"""Serviços de entrada do aplicativo."""
from __future__ import annotations

import queue
import threading
import time

from ..interfaces import IRenderer
from ._tty import _stdin, read_user_input, read_user_input_with_timeout
from ._editor import read_from_editor
from ._file import _normalize_loaded_content, read_from_file

__all__ = [
    "AppInputServices",
    "read_user_input",
    "read_user_input_with_timeout",
    "read_from_editor",
    "read_from_file",
    "_normalize_loaded_content",
    "_stdin",
]


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
