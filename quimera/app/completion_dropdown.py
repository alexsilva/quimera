"""Dropdown inline de autocomplete com filtragem e navegação por setas."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class Completion:
    """Item do dropdown: ``value`` entra no input, ``label`` é o que se vê.

    ``aliases`` são outros textos que o usuário pode digitar para alcançar o
    item (``/stop codex`` chega em ``/stop #1``). Um ``str`` simples equivale a
    ``Completion(value)``.
    """

    value: str
    label: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        return self.label or self.value

    def matches(self, prefix: str) -> bool:
        """Casa pelo prefixo do valor ou de um dos apelidos, sem distinguir caixa."""
        needle = prefix.casefold()
        return any(
            candidate.casefold().startswith(needle)
            for candidate in (self.value, *self.aliases)
        )

    def prefixed(self, prefix: str) -> Completion:
        """Antepõe o comando ao valor e aos apelidos, preservando o rótulo."""
        return Completion(
            f"{prefix}{self.value}",
            self.label,
            tuple(f"{prefix}{alias}" for alias in self.aliases),
        )


def _as_completion(item: str | Completion) -> Completion:
    return item if isinstance(item, Completion) else Completion(str(item))


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
        self._all: list[Completion] = []
        self._filtered: list[Completion] = []
        self._selected = 0
        self._widgets: list[Static] = []

    def compose(self) -> ComposeResult:
        for i in range(self.MAX_VISIBLE):
            yield Static("", id=f"cd-{i}", classes="completion-item")

    def on_mount(self) -> None:
        self._widgets = [self.query_one(f"#cd-{i}", Static) for i in range(self.MAX_VISIBLE)]

    def set_completions(self, completions: Sequence[str | Completion]) -> None:
        self._all = [_as_completion(item) for item in completions or ()]

    def filter(self, query: str) -> None:
        q = query.strip()
        self._filtered = [c for c in self._all if c.matches(q)]
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
                w.update(self._filtered[i].display)
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
            return self._filtered[self._selected].value
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
            return self._filtered[0].value
        return None
