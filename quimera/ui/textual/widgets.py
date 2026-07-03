"""Widgets Textual usados pela aplicação principal."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Input, Static
from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderIcon, HeaderTitle

from quimera.app.completion_dropdown import CompletionDropdown, PromptHistorySuggester


class _CompletionInput(Input):
    """Input com autocomplete inline: setas navegam, Tab completa, Enter completa e submete."""

    BINDINGS = [
        Binding("escape", "escape", "Fechar popup"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prompt_history: list[str] = []
        self._history_index = 0
        self._saved_draft = ""
        self.suggester = PromptHistorySuggester(lambda: self._prompt_history)

    def add_to_history(self, value: str) -> None:
        if value:
            self._prompt_history.append(value)
            self._history_index = 0
            self._saved_draft = ""

    def load_history(self, path: Path | None) -> None:
        """Carrega histórico persistente do input, quando disponível."""
        if path is None or not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        entries = []
        for line in lines:
            value = line.removeprefix("+").strip()
            if value:
                entries.append(value)
        self._prompt_history = entries[-1000:]
        self._history_index = 0
        self._saved_draft = ""

    def save_history(self, path: Path | None) -> None:
        """Persiste histórico do input para próxima sessão."""
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(self._prompt_history[-1000:]) + "\n", encoding="utf-8")
        except OSError:
            return

    async def action_submit(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        selected = dropdown.get_selected()
        if selected is not None:
            self.value = f"{selected} "
            self.cursor_position = len(self.value)
            dropdown.hide()
            return
        await super().action_submit()

    def action_escape(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        dropdown.hide()

    def key_up(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        if dropdown.has_options:
            dropdown.select_prev()
            return
        if not self._prompt_history:
            return
        if self._history_index >= len(self._prompt_history):
            return
        if self._history_index == 0:
            self._saved_draft = self.value
        self._history_index += 1
        idx = len(self._prompt_history) - self._history_index
        self.value = self._prompt_history[idx]
        self.cursor_position = len(self.value)

    def key_down(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        if dropdown.has_options:
            dropdown.select_next()
            return
        if self._history_index == 0:
            return
        self._history_index -= 1
        if self._history_index == 0:
            self.value = self._saved_draft
        else:
            idx = len(self._prompt_history) - self._history_index
            self.value = self._prompt_history[idx]
        self.cursor_position = len(self.value)

    def key_tab(self) -> None:
        dropdown = self.app.query_one(CompletionDropdown)
        selected = dropdown.get_selected()
        if selected:
            self.value = f"{selected} "
            self.cursor_position = len(self.value)
            dropdown.hide()
            return

class _SummarySpinner(Static):
    """Indicador discreto de resumo, separado do relógio."""

class _SummaryHeader(Header):
    """Header com spinner próprio antes do relógio."""

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _SummarySpinner("", id="summary-spinner")
        yield (
            HeaderClock().data_bind(Header.time_format)
            if self._show_clock
            else HeaderClockSpace()
        )
