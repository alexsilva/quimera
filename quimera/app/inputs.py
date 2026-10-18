"""Serviços e utilitários de entrada do aplicativo."""
from __future__ import annotations

import logging
import queue
import select
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from ..editor import Editor
from .interfaces import IRenderer

_logger = logging.getLogger(__name__)


def _stdin():
    """Retorna stdin atual."""
    return sys.stdin


def read_user_input(
    renderer: IRenderer,
    prompt,
    timeout: int,
    *,
    input_fn=input,
    flush_deferred_messages=lambda: None,
) -> str | None:
    """Lê entrada do usuário respeitando timeout e modo não-bloqueante.

    Em TTY com ``timeout == 0`` as mensagens diferidas são liberadas antes e
    depois da leitura bloqueante, para o humano ver o que chegou enquanto o
    prompt estava ocupado.
    """
    read_chat_line = getattr(input_fn, "read_chat_line", None)
    if callable(read_chat_line) and getattr(input_fn, "sequential", False) is True:
        return _read_sequential_input(renderer, read_chat_line, prompt, timeout)
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
                flush_deferred_messages()
                try:
                    return input_fn(prompt)
                except KeyboardInterrupt:
                    print()
                    raise
                finally:
                    flush_deferred_messages()
            if select.select([stdin], [], [], 0)[0]:
                line = stdin.readline()
                if line == "":
                    return None
                return line.rstrip("\r\n")
            time.sleep(0.01)
            return None
        except Exception as exc:
            _logger.debug("read_user_input: leitura não bloqueante falhou: %s", exc)
            return None

    try:
        return input_fn(prompt)
    except EOFError:
        if timeout == 0:
            return None
        raise
    except KeyboardInterrupt:
        print()
        raise


def _read_sequential_input(renderer: IRenderer, read_chat_line, prompt, timeout) -> str | None:
    """Stdin em pipe: o gate é o único leitor; nada aqui toca ``sys.stdin``.

    ``None`` significa "nenhuma linha ainda" (o chamador volta a perguntar);
    o fim do stdin chega como ``EOFError`` do gate.
    """
    if timeout and timeout > 0:
        value = read_chat_line(timeout=timeout, prompt=prompt)
        if value is None:
            renderer.show_system(f"*idle* ({timeout}s sem activity)")
        return value
    if timeout == 0:
        return read_chat_line(timeout=0.01, prompt=prompt)
    return read_chat_line(timeout=None, prompt=prompt)


def read_user_input_with_timeout(prompt: str, timeout: int, input_fn=input):
    """Lê entrada do usuário com timeout."""
    stdin = _stdin()
    if stdin is not None and not stdin.isatty():
        try:
            ready, _, _ = select.select([stdin], [], [], timeout)
        except Exception as exc:
            _logger.debug("read_user_input_with_timeout: select falhou: %s", exc)
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


def read_from_editor(renderer: IRenderer, output_lock=None) -> str | None:
    """Abre o editor configurado e retorna o conteúdo digitado."""
    return Editor(renderer, output_lock=output_lock).compose()


def read_from_file(renderer: IRenderer, path_str):
    """Lê conteúdo de um arquivo fornecido pelo usuário."""
    path = Path(str(path_str or "").strip()).expanduser()
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


class AppInputServices:
    """Agrupa operações de entrada e edição usadas pela aplicação."""

    def __init__(
        self,
        renderer: IRenderer,
        input_resolver,
        *,
        flush_deferred_messages=None,
        output_lock=None,
    ):
        self._renderer = renderer
        self.input_resolver = input_resolver
        self._flush_deferred_messages = flush_deferred_messages or (lambda: None)
        self._output_lock = output_lock
        self._nonblocking_tty = False
        self._input_queue: queue.Queue | None = None
        self._input_thread: threading.Thread | None = None
        self._wakeup_event: threading.Event | None = None

    def set_nonblocking_tty(self, enabled: bool) -> None:
        self._nonblocking_tty = enabled

    @property
    def sequential_input(self) -> bool:
        """True quando o gate consome stdin em ordem (pipe, sem TUI)."""
        try:
            gate = self.input_resolver()
        except Exception as exc:
            _logger.debug("sequential_input: input_resolver falhou: %s", exc)
            return False
        return bool(getattr(gate, "sequential", False))

    def set_wakeup_event(self, event: threading.Event | None) -> None:
        self._wakeup_event = event

    def read_user_input(self, prompt, timeout: int) -> str | None:
        if timeout == 0 and self._nonblocking_tty:
            stdin = _stdin()
            if stdin is not None and stdin.isatty():
                return self._read_nonblocking_tty(prompt)
        return read_user_input(
            self._renderer,
            prompt,
            timeout,
            input_fn=self.input_resolver(),
            flush_deferred_messages=self._flush_deferred_messages,
        )

    def _read_nonblocking_tty(self, prompt: str) -> str | None:
        if self._input_queue is None:
            self._input_queue = queue.Queue()
        try:
            status, value = self._input_queue.get_nowait()
        except queue.Empty:
            if self._input_thread is None or not self._input_thread.is_alive():
                self._start_input_reader(prompt)
            elif self._wakeup_event is not None:
                self._wakeup_event.wait(timeout=0.01)
                self._wakeup_event.clear()
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
        if self._input_queue is None:
            self._input_queue = queue.Queue()
        input_fn = self.input_resolver()

        def _reader() -> None:
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

        self._input_thread = threading.Thread(target=_reader, daemon=True)
        self._input_thread.start()

    def read_from_editor(self):
        return read_from_editor(self._renderer, output_lock=self._output_lock)

    def read_from_file(self, path_str):
        return read_from_file(self._renderer, path_str)


class AskUserPrompter:
    """Prompt interativo para ferramentas que precisam escolher uma opção."""

    def __init__(self, input_gate, renderer) -> None:
        self._input_gate = input_gate
        self._renderer = renderer

    def ask(self, question: str, options: list) -> tuple:
        opts = [str(o) for o in options]
        input_gate = self._input_gate
        renderer = self._renderer
        gate_is_active = (
            input_gate is not None
            and callable(getattr(input_gate, "is_active", None))
            and input_gate.is_active()
        )
        if gate_is_active:
            controller = renderer.agent_window_controller("agente") if renderer is not None else None
            if controller is not None:
                result = controller.ask_selection(renderer, input_gate, question, opts)
            else:
                result = input_gate.read_selection_in_terminal(question, opts)
            if result is not None:
                return result
            raise EOFError("sem resposta do terminal")
        selection_context = (
            renderer.selection_window(metadata={"question": question, "options": opts})
            if renderer is not None
            else nullcontext()
        )
        with selection_context:
            error_msg: str | None = None
            while True:
                parts: list[str] = []
                if error_msg:
                    parts.append(f"  ! {error_msg}")
                parts.append(f"\n{question}")
                for i, opt in enumerate(opts, 1):
                    parts.append(f"  {i}. {opt}")
                parts.append(f"  (número 1-{len(opts)} ou texto exato)")
                sys.stdout.write("\n".join(parts) + "\n> ")
                sys.stdout.flush()
                raw = sys.stdin.readline().rstrip("\n\r").strip()
                try:
                    idx = int(raw) - 1
                    if 0 <= idx < len(opts):
                        return idx, opts[idx]
                except ValueError:
                    pass
                for i, opt in enumerate(opts):
                    if opt.lower() == raw.lower():
                        return i, opt
                error_msg = f"'{raw}' não é uma opção válida."


__all__ = [
    "AskUserPrompter",
    "AppInputServices",
    "read_user_input",
    "read_user_input_with_timeout",
    "read_from_editor",
    "read_from_file",
    "_normalize_loaded_content",
    "_stdin",
]
