"""Regressões de custo que preservam a semântica/saída da TUI."""
from __future__ import annotations

import asyncio
import random
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult

from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.renderables import _live_tail
from quimera.ui.textual.widgets import _UnifiedFeed


def _reference_live_tail(text: str) -> tuple[str, str]:
    """Algoritmo anterior (e34f69c), usado como oráculo de equivalência."""
    lines = text.splitlines()
    tail = "\n".join(lines[-8:])
    if len(tail) > 1600:
        cut = tail[-1600:]
        newline = cut.find("\n")
        if 0 <= newline < len(cut) - 1:
            cut = cut[newline + 1:]
        tail = cut
    if tail == text:
        return text, ""
    hidden = len(lines) - (tail.count("\n") + 1)
    if hidden > 0:
        return tail, f"⋮ +{hidden} linhas anteriores"
    return tail, "⋮ trecho anterior oculto"


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("trailing", [False, True])
def test_live_tail_preserves_all_splitlines_boundaries(separator, trailing):
    text = separator.join(f"linha {index}: " + "ação 🙂 " * 20 for index in range(200))
    if trailing:
        text += separator
    assert len(text) > 16000
    assert _live_tail(text) == _reference_live_tail(text)


@pytest.mark.parametrize("text", [
    "", "\n", "\n" * 20000, "x" * 20000,
    "a\n" * 10000, "a\n" * 10000 + "fim\n",
    "primeira\n" + "x" * 20000 + "\nfim",
    "antiga\n" * 3000 + "x" * 1599 + "\n",
    "antiga\n" * 3000 + "x" * 1600 + "\n",
    "antiga\n" * 3000 + "x" * 1601 + "\n",
    "🙂" * 20000,
])
def test_live_tail_matches_previous_output_at_truncation_edges(text):
    assert _live_tail(text) == _reference_live_tail(text)


def test_live_tail_matches_previous_output_for_deterministic_mixed_documents():
    rng = random.Random(385)
    for _ in range(100):
        lines = ["x" * rng.randrange(0, 5000) for _ in range(rng.randrange(1, 25))]
        text = "\n".join(lines) + ("\n" if rng.randrange(2) else "")
        assert _live_tail(text) == _reference_live_tail(text)


def _user_event(submission_id: str, status: str, revision: int = 0) -> TextualUiEvent:
    return TextualUiEvent("user_message", {
        "content": submission_id,
        "submission_id": submission_id,
        "submission": {"status": status, "revision": revision},
    })


def _assert_active_slots_match_scan(model):
    expected = []
    for index, item in enumerate(model.items):
        if item.event.kind == "user_message":
            submission = item.event.payload.get("submission", {})
            status = str(submission.get("status") or "").strip().lower()
            if status and status not in {"completed", "failed", "cancelled"}:
                expected.append((index, item))
    assert model.active_submission_items() == expected


def test_active_submission_pulse_does_not_iterate_over_history():
    model = TextualFeedModel()
    for index in range(1000):
        model.apply(_user_event(str(index), "completed"))
    model.apply(_user_event("live", "running"))

    class NoScanList(list):
        def __iter__(self):
            raise AssertionError("o pulso não deve varrer o scrollback")

    model._items = NoScanList(model._items)
    active = model.active_submission_items()
    assert len(active) == 1
    assert active[0][0] == 1000
    # O snapshot retornado continua independente do índice interno.
    active.clear()
    assert len(model.active_submission_items()) == 1


@pytest.mark.parametrize("status,active", [
    ("accepted", True), ("queued", True), (" RUNNING ", True),
    ("waiting", True), ("starting", True), ("unknown", True),
    ("completed", False), ("failed", False), ("cancelled", False), ("", False),
])
def test_active_submission_index_preserves_status_normalization(status, active):
    model = TextualFeedModel()
    model.apply(_user_event("one", status))
    assert bool(model.active_submission_items()) == active


def test_active_submission_index_follows_revisions_and_terminal_transitions():
    model = TextualFeedModel()
    model.apply(_user_event("one", "running", revision=1))
    assert model.apply(TextualUiEvent("submission_status", {
        "submission_id": "one", "status": "completed", "revision": 2,
    }))
    assert model.active_submission_items() == []
    assert not model.apply(TextualUiEvent("submission_status", {
        "submission_id": "one", "status": "running", "revision": 1,
    }))
    assert model.active_submission_items() == []
    model.apply(TextualUiEvent("submission_status", {
        "submission_id": "one", "status": "waiting", "revision": 3,
    }))
    active = model.active_submission_items()
    assert active == [(0, model.items[0])]
    assert active[0][1].event.payload["submission"]["revision"] == 3


def test_active_submission_index_follows_boot_delegation_insertion_and_removal():
    model = TextualFeedModel()
    model.apply(_user_event("first", "accepted"))
    model.apply(TextualUiEvent("delegation", {
        "delegation_id": "dlg", "from_label": "A", "to_label": "B",
    }, agent="parent"))
    model.apply(_user_event("second", "running"))
    model.apply(TextualUiEvent("muted", "boot tardio", boot=True, compact=True))
    _assert_active_slots_match_scan(model)
    model.apply(TextualUiEvent("agent_update", {"delegation_id": "dlg", "content": "gerando..."}, agent="child"))
    _assert_active_slots_match_scan(model)
    model.apply(TextualUiEvent("agent_message", {
        "delegation_id": "dlg", "content": "fim",
    }, agent="child"))
    _assert_active_slots_match_scan(model)
    model.apply(TextualUiEvent("agent_update", {"content": "executando"}, agent="other"))
    model.apply(_user_event("third", "waiting"))
    model.apply(TextualUiEvent("visual_reset"))
    _assert_active_slots_match_scan(model)
    model.clear_chat()
    assert model.active_submission_items() == []
    assert all(item.event.boot for item in model.items)
    model.apply(_user_event("next", "accepted"))
    _assert_active_slots_match_scan(model)


def test_active_submission_index_ignores_restored_history_and_untracked_turns():
    model = TextualFeedModel()
    model.apply(_user_event("live", "running"))
    model.hydrate_from_history([{"role": "user", "content": "antigo"}])
    model.apply(TextualUiEvent("user_message", {"content": "sem status"}))
    model.apply(TextualUiEvent("user_message", "texto simples"))
    assert model.active_submission_items() == [(0, model.items[0])]


def test_stable_feed_slots_skip_css_validation_without_losing_class_changes():
    class FeedApp(App):
        def compose(self) -> ComposeResult:
            yield _UnifiedFeed(id="feed")

    async def exercise():
        async with FeedApp().run_test(size=(56, 22)) as pilot:
            feed = pilot.app.query_one("#feed", _UnifiedFeed)
            entries = [(1, False, "boot", True), (2, False, "chat", False)]
            feed.sync_entries(entries)
            await pilot.pause()
            first, second = feed._entry_widgets
            with patch.object(first, "set_class", wraps=first.set_class) as first_class, patch.object(
                second, "set_class", wraps=second.set_class,
            ) as second_class, patch.object(second, "update", wraps=second.update) as update:
                feed.sync_entries(entries)
                first_class.assert_not_called()
                second_class.assert_not_called()
                update.assert_not_called()
                # Compact pode mudar até quando o token permanece igual.
                feed.sync_entries([(1, False, "boot", True), (2, False, "chat", True)])
                second_class.assert_called_once_with(True, "-compact")
                assert second.has_class("-compact")
                feed.sync_entries(entries, force=True)
                update.assert_called_once_with("chat")
                assert not second.has_class("-compact")

    asyncio.run(exercise())
