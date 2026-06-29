"""Dropdown inline de autocomplete com filtragem e navegação por setas."""
from __future__ import annotations

from collections.abc import Callable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.suggester import Suggester
from textual.widgets import Static


def history_suggestion_for(history: list[str], value: str) -> str | None:
    """Retorna a sugestão mais recente do histórico para o prefixo digitado."""
    prefix = str(value or "")
    if not prefix:
        return None
    for entry in reversed(history):
        candidate = str(entry or "")
        if candidate == prefix:
            continue
        if candidate.startswith(prefix):
            return candidate
    return None


class PromptHistorySuggester(Suggester):
    """Suggester Textual que replica AutoSuggestFromHistory do prompt antigo."""

    def __init__(self, history_provider: Callable[[], list[str]]) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._history_provider = history_provider

    async def get_suggestion(self, value: str) -> str | None:
        """Busca no histórico do input a continuação mais recente para value."""
        return history_suggestion_for(list(self._history_provider() or []), value)


class CompletionDropdown(Vertical):
    """Dropdown de autocomplete com filtragem por digitação e navegação por setas."""

    MAX_VISIBLE = 15

    DEFAULT_CSS = """
    CompletionDropdown {
        display: none;
        max-height: 10;
        background: $panel;
        border: tall $primary;
        overflow-y: auto;
        margin: 0 1;
    }
    CompletionDropdown.-show {
        display: block;
    }
    .completion-item {
        padding: 0 1;
    }
    .completion-item.-mark {
        background: $accent;
        color: $text;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._all: list[str] = []
        self._filtered: list[str] = []
        self._selected = 0
        self._widgets: list[Static] = []

    def compose(self) -> ComposeResult:
        for i in range(self.MAX_VISIBLE):
            yield Static("", id=f"cd-{i}", classes="completion-item")

    def on_mount(self) -> None:
        self._widgets = [self.query_one(f"#cd-{i}") for i in range(self.MAX_VISIBLE)]

    def set_completions(self, completions: list[str]) -> None:
        self._all = completions or []

    def filter(self, query: str) -> None:
        q = query.strip().lower()
        if not q:
            self._filtered = list(self._all)
        else:
            self._filtered = [c for c in self._all if c.lower().startswith(q)]
        self._selected = 0
        self._refresh()

    def _visible_count(self) -> int:
        return min(len(self._filtered), self.MAX_VISIBLE)

    def _refresh(self) -> None:
        shown = len(self._filtered) > 0
        self.set_class(shown, "-show")
        if not shown:
            return
        visible = self._visible_count()
        for i, w in enumerate(self._widgets):
            if i < visible:
                w.update(self._filtered[i])
                w.display = True
                w.set_class(i == self._selected, "-mark")
            else:
                w.display = False

    def select_next(self) -> None:
        if self._visible_count() < 2:
            return
        self._widgets[self._selected].set_class(False, "-mark")
        self._selected = (self._selected + 1) % self._visible_count()
        self._widgets[self._selected].set_class(True, "-mark")

    def select_prev(self) -> None:
        if self._visible_count() < 2:
            return
        self._widgets[self._selected].set_class(False, "-mark")
        self._selected = (self._selected - 1) % self._visible_count()
        self._widgets[self._selected].set_class(True, "-mark")

    def get_selected(self) -> str | None:
        if self._filtered:
            return self._filtered[self._selected]
        return None

    def hide(self) -> None:
        self.set_class(False, "-show")
        self._filtered = []
        self._selected = 0

    @property
    def has_options(self) -> bool:
        return len(self._filtered) > 1

    def single_match(self) -> str | None:
        if len(self._filtered) == 1:
            return self._filtered[0]
        return None
