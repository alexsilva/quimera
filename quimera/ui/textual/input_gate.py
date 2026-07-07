"""Input gate da interface Textual."""
from __future__ import annotations

import queue
import threading

from rich.cells import cell_len
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

    @staticmethod
    def _clip_toolbar_middle(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        if max_len <= 1:
            return "…"
        head_len = max(1, (max_len - 1) // 2)
        tail_len = max(1, max_len - 1 - head_len)
        return f"{value[:head_len].rstrip()}…{value[-tail_len:].lstrip()}"

    @staticmethod
    def _toolbar_plain_width(labels: list[str]) -> int:
        if not labels:
            return 0
        chip_padding = 2 * len(labels)
        separators = max(0, len(labels) - 1)
        outer_padding = 2
        return outer_padding + separators + chip_padding + sum(cell_len(label) for label in labels)

    def _current_responder(self) -> str:
        context = self._toolbar_context()
        return str(context.get("responder", "")).strip() or ">>>"

    def _toolbar_segments(self, max_width: int | None = None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
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

        specs: list[dict[str, object]] = []

        def add(label_prefix: str, value: str, kind: str, max_len: int, min_len: int, *, middle: bool = False) -> None:
            if value:
                specs.append(
                    {
                        "prefix": label_prefix,
                        "value": value,
                        "kind": kind,
                        "max_len": max_len,
                        "min_len": min(min_len, max_len),
                        "middle": middle,
                    }
                )

        add("", responder, "accent", 24, 8)
        add("", model, "model", 24, 8)
        add("⎇ ", branch, "info", 20, 8)
        add("⚙ ", active_agents, "info", 30, 8)
        add("⚡ ", parallel, "info", 12, 3)
        add("✗ ", open_bugs, "err", 6, 1)
        add("↺ ", turns, "dim", 6, 2)
        add("◈ ", mode, "dim", 12, 4)
        add("✨ ", theme, "dim", 12, 4)
        add("🔗 ", session_id, "dim", 28, 12, middle=True)

        budgets = [int(spec["max_len"]) for spec in specs]

        def labels_for_current_budgets() -> list[str]:
            labels: list[str] = []
            for spec, budget in zip(specs, budgets):
                value = str(spec["value"])
                if bool(spec["middle"]):
                    value = self._clip_toolbar_middle(value, budget)
                else:
                    value = self._clip_toolbar_value(value, budget)
                labels.append(f"{spec['prefix']}{value}")
            return labels

        if max_width is not None and max_width > 0:
            shrink_order = [3, 1, 2, 0, 8, 7, 9, 4, 5, 6]
            while self._toolbar_plain_width(labels_for_current_budgets()) > max_width:
                shrunk = False
                for index in shrink_order:
                    if index >= len(specs):
                        continue
                    min_len = int(specs[index]["min_len"])
                    if budgets[index] > min_len:
                        budgets[index] -= 1
                        shrunk = True
                        break
                if not shrunk:
                    break

        labels = labels_for_current_budgets()
        split_at = sum(1 for value in (responder, model, branch, active_agents, parallel) if value)
        left = [(label, str(spec["kind"])) for label, spec in zip(labels[:split_at], specs[:split_at])]
        right = [(label, str(spec["kind"])) for label, spec in zip(labels[split_at:], specs[split_at:])]
        return left, right

    def _build_toolbar_text(self) -> str:
        if self._interactive_prompt_active:
            return "Enter: confirmar  |  Ctrl+C: cancelar"
        left, right = self._toolbar_segments()
        parts = [label for label, _ in left]
        parts.extend(label for label, _ in right)
        return "  |  ".join(parts)

    def _build_toolbar_renderable(self, max_width: int | None = None):
        if self._interactive_prompt_active:
            return Text("Enter: confirmar  |  Ctrl+C: cancelar", style="bold yellow on #1a1a1a")
        left, right = self._toolbar_segments(max_width=max_width)
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
        for index, (label, kind) in enumerate((*left, *right)):
            if index > 0:
                text.append(" ", style="on #1a1a1a")
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

