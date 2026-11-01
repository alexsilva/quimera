"""Regressões da identidade monotônica dos itens do feed Textual."""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from quimera.ui.textual.app import QuimeraTextualApp
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel


class _SingleSlotFeed:
    """Modelo mínimo da regra de atualização de slot de ``_UnifiedFeed``."""

    def __init__(self) -> None:
        self.token: int | None = None
        self.renderable = None

    def sync_entries(self, entries, *, force: bool = False) -> None:
        token, transient, renderable, _compact = entries[0]
        if force or transient or token != self.token:
            self.renderable = renderable
        self.token = token


def _render_text(renderable) -> str:
    stream = StringIO()
    Console(file=stream, width=80, color_system=None).print(renderable)
    return stream.getvalue()


def test_feed_item_sequences_are_unique_and_increase_across_replacements():
    model = TextualFeedModel()
    model.apply(TextualUiEvent("plain", "persistente"))
    persistent = model.items[0]
    model.apply(
        TextualUiEvent(
            "agent_update",
            {"content": "primeiro"},
            agent="codex",
        )
    )
    first_transient = model.items[1]
    model.apply(
        TextualUiEvent(
            "agent_update",
            {"content": "segundo"},
            agent="codex",
        )
    )
    replacement = model.items[1]

    assert model.items[0] is persistent
    assert persistent.seq < first_transient.seq < replacement.seq
    assert len({persistent.seq, first_transient.seq, replacement.seq}) == 3


def test_sync_feed_does_not_reuse_cached_content_when_object_ids_collide():
    app = QuimeraTextualApp(SimpleNamespace(), TextualUiBridge())
    feed = _SingleSlotFeed()
    app.query_one = lambda *_args, **_kwargs: feed

    # O patch torna a colisão determinística no código antigo, que usava
    # ``id(item.event)`` tanto no cache quanto no token do slot.
    with patch("quimera.ui.textual.app.id", return_value=41, create=True):
        app._feed_model.apply(TextualUiEvent("plain", "conteúdo antigo"))
        app._sync_feed()
        assert "conteúdo antigo" in _render_text(feed.renderable)

        app._feed_model.clear_chat()
        app._feed_model.apply(TextualUiEvent("plain", "conteúdo novo"))
        app._sync_feed()

    output = _render_text(feed.renderable)
    assert "conteúdo novo" in output
    assert "conteúdo antigo" not in output
