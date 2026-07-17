"""Input gate baseado em input() para modo não-interativo (pipe, redirecionamento).

Usado quando o app roda sem TTY, substituindo o TextualInputGate que depende
de uma UI Textual rodando para fornecer input via fila.
"""
from __future__ import annotations

import threading


class SimpleInputGate:
    """Input gate que usa input() padrão do Python.

    Mantém a mesma interface pública de TextualInputGate para compatibilidade
    com os consumidores (InputBroker, ApprovalHandler, etc.), mas sem depender
    de prompt_toolkit ou de uma UI Textual.
    """

    def __init__(
        self,
        renderer=None,
        toolbar_context_resolver=None,
        history_file=None,
        command_resolver=None,
        argument_resolver=None,
    ) -> None:
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver
        self._theme_cycle_handler = None
        self._active_lock = threading.Lock()
        self._active = False
        self._owner_thread_id: int | None = None

    def set_toolbar_context_resolver(self, resolver) -> None:
        self._toolbar_context_resolver = resolver

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_argument_resolver(self, resolver) -> None:
        self._argument_resolver = resolver

    def set_theme_cycle_handler(self, handler) -> None:
        self._theme_cycle_handler = handler

    def is_active(self) -> bool:
        with self._active_lock:
            return self._active

    def get_owner_thread_id(self) -> int | None:
        with self._active_lock:
            return self._owner_thread_id

    def run_in_terminal_message(self, callback) -> bool:
        return False

    def redisplay(self) -> None:
        pass

    def get_line_buffer(self) -> str:
        return ""

    def _set_active_state(self, active: bool) -> None:
        with self._active_lock:
            self._active = active
            self._owner_thread_id = threading.get_ident() if active else None

    def __call__(self, prompt: str) -> str:
        self._set_active_state(True)
        try:
            return input(prompt)
        finally:
            self._set_active_state(False)

    def read_plain_input(self, prompt: str) -> str:
        return self(prompt)

    def read_input_in_terminal(
        self,
        prompt: str,
        timeout: float = 300.0,
        render_card_fn=None,
        owner: str | None = None,
    ) -> str | None:
        return self(prompt)

    def read_selection_in_terminal(
        self,
        question: str,
        options: list[str],
        timeout: float = 300.0,
        owner: str | None = None,
    ) -> tuple[int, str] | None:
        print(question)
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
        raw = input(f"Selecione (1-{len(options)}): ").strip()
        try:
            index = int(raw) - 1
            if 0 <= index < len(options):
                return index, options[index]
        except ValueError:
            pass
        for index, option in enumerate(options):
            if option.lower() == raw.lower():
                return index, option
        return None

    def read_approval_in_terminal(
        self,
        question: str,
        prompt: str,
        timeout: float = 300.0,
        render_card_fn=None,
        owner: str | None = None,
    ) -> str | None:
        print(question)
        return input(prompt).strip().lower() or None
