"""Input gate da interface Textual."""
from __future__ import annotations

import queue
import threading

from rich.text import Text

from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.constants import (
    APPROVAL_OPTIONS as _APPROVAL_OPTIONS,
    APPROVAL_TITLE as _APPROVAL_TITLE,
)
from quimera.ui.textual.events import TextualUiEvent


def _approval_options() -> list[str]:
    """Retorna opções de aprovação exibidas no prompt inline."""
    return list(_APPROVAL_OPTIONS)


class TextualInputGate:
    """Input gate por fila para a TUI Textual."""

    def __init__(
        self,
        bridge: TextualUiBridge,
        renderer=None,
        toolbar_context_resolver=None,
        history_file=None,
        command_resolver=None,
        argument_resolver=None,
    ) -> None:
        self._bridge = bridge
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver
        self._theme_cycle_handler = None
        self._history_file = history_file
        self._textual_mounted = False
        self._active_lock = threading.Lock()
        self._active = False
        self._interactive_prompt_active = False
        self._owner_thread_id: int | None = None

    def set_toolbar_context_resolver(self, resolver) -> None:
        """Define callback para contexto dinâmico da toolbar."""
        self._toolbar_context_resolver = resolver

    def set_command_resolver(self, resolver) -> None:
        """Define callback para comandos disponíveis."""
        self._command_resolver = resolver

    def set_argument_resolver(self, resolver) -> None:
        """Define callback para argumentos de comandos."""
        self._argument_resolver = resolver

    def set_theme_cycle_handler(self, handler) -> None:
        """Define callback de troca de tema."""
        self._theme_cycle_handler = handler

    def is_active(self) -> bool:
        """Indica se o loop está aguardando input humano."""
        with self._active_lock:
            return self._active or self._textual_mounted

    def set_textual_mounted(self, mounted: bool) -> None:
        """Indica que a UI Textual está pronta para receber input interativo."""
        with self._active_lock:
            self._textual_mounted = bool(mounted)

    def get_owner_thread_id(self) -> int | None:
        """Retorna o thread atualmente bloqueado aguardando input."""
        with self._active_lock:
            return self._owner_thread_id

    def run_in_terminal_message(self, callback) -> bool:
        """Compatibilidade: Textual não usa run_in_terminal."""
        return False

    def redisplay(self) -> None:
        """Atualiza toolbar/sugestões enquanto o input Textual está ativo."""
        if not self.is_active():
            return None
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": "",
                    "responder": self._current_responder(),
                    "toolbar": self._build_toolbar_renderable(),
                    "commands": self._commands(),
                },
            )
        )
        return None

    def get_line_buffer(self) -> str:
        """Compatibilidade com callers que consultam buffer atual."""
        return self._bridge.get_input_value()

    def clear_interactive_prompt_state(self) -> None:
        """Força limpeza visual do estado de prompt interativo."""
        self._interactive_prompt_active = False

    def _set_active_state(self, active: bool) -> None:
        with self._active_lock:
            self._active = active
            self._owner_thread_id = threading.get_ident() if active else None
        self._bridge.emit(TextualUiEvent("input_active", active))

    def _toolbar_context(self) -> dict[str, str]:
        resolver = self._toolbar_context_resolver
        if callable(resolver):
            try:
                context = dict(resolver() or {})
            except Exception:
                context = {}
        else:
            context = {}
        active_agent = self._bridge.active_agent_label()
        if active_agent and not str(context.get("active_agents", "")).strip():
            context["active_agents"] = active_agent
        return context

    @staticmethod
    def _clip_toolbar_value(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max(1, max_len - 1)].rstrip() + "…"

    def _current_responder(self) -> str:
        context = self._toolbar_context()
        return str(context.get("responder", "")).strip() or ">>>"

    def _toolbar_segments(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        context = self._toolbar_context()
        responder = str(context.get("responder", "")).strip()
        model = str(context.get("model", "")).strip()
        branch = str(context.get("branch", "")).strip()
        active_agents = str(context.get("active_agents", "")).strip()
        parallel = str(context.get("parallel", "")).strip()
        open_bugs = str(context.get("open_bugs", "")).strip()
        mode = str(context.get("mode", "")).strip()
        turns = str(context.get("turns", "")).strip()
        session_id = str(context.get("session", "")).strip()
        theme = str(context.get("theme", "")).strip()

        left: list[tuple[str, str]] = []
        if responder:
            left.append((self._clip_toolbar_value(responder, 24), "accent"))
        if model:
            left.append((self._clip_toolbar_value(model, 24), "model"))
        if branch:
            left.append((f"⎇ {self._clip_toolbar_value(branch, 20)}", "info"))
        if active_agents:
            left.append((f"⚙ {self._clip_toolbar_value(active_agents, 30)}", "info"))
        if parallel:
            left.append((f"⚡ {parallel}", "info"))

        right: list[tuple[str, str]] = []
        if open_bugs:
            right.append((f"✗ {open_bugs}", "err"))
        if turns:
            right.append((f"↺ {turns}", "dim"))
        if mode:
            right.append((f"◈ {mode}", "dim"))
        if theme:
            right.append((f"✨ {self._clip_toolbar_value(theme, 12)}", "dim"))
        if session_id:
            right.append((f"🔗 {self._clip_toolbar_value(session_id, 22)}", "dim"))
        return left, right

    def _build_toolbar_text(self) -> str:
        if self._interactive_prompt_active:
            return "Enter: confirmar  |  Ctrl+C: cancelar"
        left, right = self._toolbar_segments()
        parts = [label for label, _ in left]
        parts.extend(label for label, _ in right)
        return "  |  ".join(parts)

    def _build_toolbar_renderable(self):
        if self._interactive_prompt_active:
            return Text("Enter: confirmar  |  Ctrl+C: cancelar", style="bold yellow on #1a1a1a")
        left, right = self._toolbar_segments()
        if not left and not right:
            return Text("")
        style_by_kind = {
            "accent": "bold #5fc3ff on #3e3e3e",
            "model": "#9cdcfe on #3e3e3e",
            "info": "#d4d4d4 on #3e3e3e",
            "dim": "#9e9e9e on #3e3e3e",
            "err": "bold #fc7b5f on #3e3e3e",
        }

        text = Text(" ", style="on #1a1a1a")
        for label, kind in (*left, *right):
            text.append(f" {label} ", style=style_by_kind.get(kind, "white on #3e3e3e"))
        text.append(" ", style="on #1a1a1a")
        return text

    def _commands(self) -> list[str]:
        resolver = self._command_resolver
        if not callable(resolver):
            return []
        try:
            return sorted(set(str(item) for item in (resolver() or [])))
        except Exception:
            return []

    def completions_for(self, value: str, cursor_position: int | None = None) -> list[str]:
        """Retorna sugestões compatíveis com o autocomplete do prompt antigo."""
        if cursor_position is None:
            cursor_position = len(value)
        text_before_cursor = (value[:cursor_position] or "").lstrip()
        for prefix in ("s/", "r/", "o/"):
            if text_before_cursor.startswith(prefix):
                partial = text_before_cursor[len(prefix):]
                suggestions = self._argument_suggestions(prefix.rstrip("/"), partial)
                return [f"{prefix}{suggestion}" for suggestion in suggestions]
        if not text_before_cursor.startswith("/"):
            return []
        if " " in text_before_cursor:
            command, partial = text_before_cursor.split(" ", 1)
            return [
                f"{command} {suggestion}"
                for suggestion in self._argument_suggestions(command, partial)
            ]
        return [command for command in self._commands() if command.startswith(text_before_cursor)]

    def _argument_suggestions(self, command: str, partial: str) -> list[str]:
        resolver = self._argument_resolver
        if not callable(resolver):
            return []
        try:
            suggestions = resolver(command, partial) or []
        except Exception:
            return []
        return [str(item) for item in suggestions if str(item).startswith(partial)]

    def __call__(self, prompt: str) -> str:
        """Bloqueia o loop do Quimera até o usuário submeter uma linha na TUI."""
        self._set_active_state(True)
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": prompt,
                    "responder": self._current_responder(),
                    "toolbar": self._build_toolbar_renderable(),
                    "commands": self._commands(),
                },
            )
        )
        try:
            return self._bridge.input_queue.get()
        finally:
            self._set_active_state(False)
            self._bridge.emit(TextualUiEvent("prompt_clear"))

    def _read_with_textual_prompt(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        question: str | None = None,
        options: list[str] | None = None,
        owner: str | None = None,
        kind: str = "input",
        title: str | None = None,
    ) -> str | None:
        """Exibe um pedido interativo no Textual e lê uma submissão do input fixo."""
        self._bridge.begin_direct_input()
        if question is not None:
            self._interactive_prompt_active = True
            self._bridge.emit(
                TextualUiEvent(
                    "question",
                    {
                        "question": question,
                        "options": list(options or []),
                        "owner": owner,
                        "kind": kind,
                        "title": title,
                    },
                )
            )
        self._set_active_state(True)
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": prompt,
                    "responder": self._current_responder(),
                    "toolbar": self._build_toolbar_renderable(),
                    "commands": self._commands(),
                },
            )
        )
        try:
            if timeout is None:
                return self._bridge.direct_input_queue.get()
            return self._bridge.direct_input_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._set_active_state(False)
            self._bridge.end_direct_input()
            if question is not None:
                self._interactive_prompt_active = False
                self._bridge.emit(TextualUiEvent("question_clear"))
            self._bridge.emit(TextualUiEvent("prompt_clear"))

    def read_plain_input(self, prompt: str) -> str:
        """Lê uma linha simples pelo mesmo input Textual."""
        return self(prompt)

    def read_input_in_terminal(self, prompt: str, timeout: float = 300.0) -> str | None:
        """Compatibilidade: lê pelo input fixo do Textual, sem stdin direto."""
        return self._read_with_textual_prompt(prompt, timeout=timeout)

    def read_selection_in_terminal(
        self,
        question: str,
        options: list[str],
        timeout: float = 300.0,
        owner: str | None = None,
    ) -> tuple[int, str] | None:
        """Lê seleção por número/texto no input Textual."""
        raw = self._read_with_textual_prompt(
            f"Selecione (1-{len(options)}): ",
            timeout=timeout,
            question=question,
            options=options,
            owner=owner,
            kind="selection",
        )
        if raw is None:
            return None
        raw = raw.strip()
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
        owner: str | None = None,
    ) -> str | None:
        """Lê aprovação pelo input Textual."""
        return self._read_with_textual_prompt(
            prompt,
            timeout=timeout,
            question=question,
            options=_approval_options(),
            owner=owner,
            kind="approval",
            title=_APPROVAL_TITLE,
        )

