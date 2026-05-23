"""Leitura de entrada via TTY — bloqueante, com timeout e não-bloqueante."""
from __future__ import annotations

import queue
import select
import sys
import threading
import time

from ..interfaces import IRenderer


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


def _stdin():
    """Executa stdin."""
    return sys.stdin
