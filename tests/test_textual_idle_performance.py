"""Custo em idle da TUI Textual: widgets de status invisíveis e timer do pulso.

Os três custos recorrentes de ``quimera/ui/textual/app.py`` quando nada muda
na tela eram:

- ``update("")`` nos Statics ``#status_bar``/``#agent_status`` a cada poll de
  0,2 s, que pedia relayout da tela inteira sem mudança visível;
- ticks do timer do pulso de pensamento (0,25 s) sem transients nem
  submissões ativas;
- rajadas de eventos reescrevendo os mesmos Statics já vazios.

Os testes contam chamadas de forma determinística (app real capturado sem
loop de terminal, widgets mock e timer fake) e, no caso headless, verificam o
estado real do ``Timer`` do Textual sem depender de quantos ticks o host
lento consegue entregar.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import quimera.ui.textual.renderables as renderables
from quimera.ui.textual.app import run_textual_quimera_app
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel


@pytest.fixture(autouse=True)
def _reset_thinking_pulse():
    renderables.reset_thinking_pulse()
    yield
    renderables.reset_thinking_pulse()


def _capture_app(quimera_app, bridge: TextualUiBridge):
    """Constrói o QuimeraTextualApp real sem entrar no loop do terminal."""
    captured = []
    with patch("textual.app.App.run", lambda self, *args, **kwargs: captured.append(self)):
        run_textual_quimera_app(quimera_app, bridge)
    return captured[0]


def _mock_widgets() -> dict[str, SimpleNamespace]:
    return {
        "#feed": SimpleNamespace(
            is_vertical_scroll_end=False,
            sync_entries=Mock(),
            update_entry=Mock(return_value=True),
            anchor=Mock(),
        ),
        "#toolbar": SimpleNamespace(size=SimpleNamespace(width=80), update=Mock()),
        "#status_bar": SimpleNamespace(display=True, update=Mock()),
        "#agent_status": SimpleNamespace(display=True, update=Mock()),
        "#breadcrumb": SimpleNamespace(update=Mock()),
        "#input": SimpleNamespace(focus=Mock()),
    }


def _app_with_mock_widgets():
    bridge = TextualUiBridge()
    quimera_app = SimpleNamespace(
        renderer=None,
        input_gate=SimpleNamespace(_build_toolbar_renderable=Mock(return_value="toolbar")),
    )
    app = _capture_app(quimera_app, bridge)
    widgets = _mock_widgets()
    app.query_one = Mock(side_effect=lambda selector, *_: widgets[selector])
    app.refresh = Mock()
    app.call_after_refresh = Mock()
    return app, bridge, widgets


class _FakeTimer:
    """Substituto do Timer do Textual que só conta pause/reset."""

    def __init__(self) -> None:
        self.pause = Mock()
        self.resume = Mock()
        self.reset = Mock()
        self.stop = Mock()


def _running_submission(submission_id: str = "s1") -> TextualUiEvent:
    return TextualUiEvent(
        "user_message",
        {
            "content": "revise o módulo",
            "submission_id": submission_id,
            "submission": {"status": "running", "revision": 1},
        },
    )


# --- Statics de status invisíveis -------------------------------------------


def test_idle_polls_blank_hidden_status_widgets_only_once():
    app, _bridge, widgets = _app_with_mock_widgets()

    for _ in range(50):
        app._poll_active_agent()

    assert widgets["#agent_status"].update.call_count == 1
    assert widgets["#agent_status"].update.call_args.args == ("",)
    assert widgets["#status_bar"].update.call_count == 1
    assert widgets["#status_bar"].update.call_args.args == ("",)
    assert widgets["#agent_status"].display is False
    assert widgets["#status_bar"].display is False


def test_prompt_cycles_and_bursts_do_not_rewrite_blank_status_widgets():
    app, bridge, widgets = _app_with_mock_widgets()
    app._poll_active_agent()

    for index in range(20):
        bridge.emit(TextualUiEvent("prompt", {"toolbar": "", "commands": []}))
        bridge.emit(TextualUiEvent("plain", f"linha {index}"))
        app._drain_bridge_events()
        app._poll_active_agent()

    assert widgets["#agent_status"].update.call_count == 1
    assert widgets["#status_bar"].update.call_count == 1
    # O estado lógico continua rastreado; só a escrita no widget é suprimida.
    assert app._last_status_bar_state == (None, ())
    # O prompt segue pedindo o relayout explícito que já pedia antes.
    assert app.refresh.call_count == 20


def test_status_state_change_is_tracked_without_rewriting_hidden_widgets():
    app, bridge, widgets = _app_with_mock_widgets()
    app._poll_active_agent()

    bridge.emit(TextualUiEvent("tool_preview", {"content": "⚒ grep_search"}, agent="claude"))
    app._drain_bridge_events()
    app._poll_active_agent()

    assert app._last_status_bar_state == (None, (("claude", "⚒ grep_search"),))
    assert app.active_agent == "claude"
    assert widgets["#status_bar"].update.call_count == 1
    assert widgets["#agent_status"].update.call_count == 1


def test_missing_status_widget_keeps_returning_false():
    app, _bridge, widgets = _app_with_mock_widgets()
    widgets.pop("#agent_status")
    app.query_one = Mock(side_effect=_raise_no_matches)

    assert app._update_agent_status_widget() is False
    assert app._blank_status_selectors == set()
    # Uma consulta anterior à montagem não deve envenenar o cache do helper.
    recovered = SimpleNamespace(display=True, update=Mock())
    app.query_one = Mock(return_value=recovered)
    assert app._update_agent_status_widget() is True
    assert app._update_agent_status_widget() is True
    recovered.update.assert_called_once_with("")


def _raise_no_matches(selector, *_):
    from textual.css.query import NoMatches

    raise NoMatches(selector)


# --- Timer do pulso de pensamento --------------------------------------------


def test_pulse_pauses_timer_when_idle_and_is_idempotent():
    app, _bridge, _widgets = _app_with_mock_widgets()
    timer = _FakeTimer()
    app._thinking_pulse_timer = timer

    app._pulse_thinking_marker()
    app._pulse_thinking_marker()
    app._pulse_thinking_marker()

    assert timer.pause.call_count == 1
    assert app._thinking_pulse_paused is True
    timer.reset.assert_not_called()
    timer.resume.assert_not_called()
    assert renderables._thinking_pulse_index == 0


def test_pulse_without_timer_is_safe():
    app, _bridge, _widgets = _app_with_mock_widgets()
    assert app._thinking_pulse_timer is None

    app._pulse_thinking_marker()
    assert app._thinking_pulse_paused is False
    app._sync_feed()
    assert app._thinking_pulse_paused is False


def test_feed_sync_resumes_pulse_and_transient_keeps_rhythm_untouched():
    app, bridge, widgets = _app_with_mock_widgets()
    timer = _FakeTimer()
    app._thinking_pulse_timer = timer
    app._pulse_thinking_marker()
    assert app._thinking_pulse_paused is True

    bridge.emit(TextualUiEvent("agent_update", {"label": "claude", "content": "gerando..."}, agent="claude"))
    bridge.emit(TextualUiEvent("agent_update", {"content": "pensando..."}, agent="claude"))
    app._drain_bridge_events()

    # reset() reativa o timer e recomeça a grade; resume() daria um tick atrasado.
    assert timer.reset.call_count == 1
    timer.resume.assert_not_called()
    assert app._thinking_pulse_paused is False

    for _ in range(3):
        app._pulse_thinking_marker()

    # Com transient vivo o timer não é pausado nem reiniciado: ritmo intacto.
    assert timer.pause.call_count == 1
    assert timer.reset.call_count == 1
    assert renderables._thinking_pulse_index == 3
    assert widgets["#feed"].update_entry.call_count == 3

    bridge.emit(TextualUiEvent("agent_message", {"content": "pronto"}, agent="claude"))
    app._drain_bridge_events()
    assert not app._feed_model.has_transients
    assert timer.reset.call_count == 1

    app._pulse_thinking_marker()
    assert timer.pause.call_count == 2
    assert app._thinking_pulse_paused is True
    assert renderables._thinking_pulse_index == 0


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_pulse_keeps_running_for_active_submission_and_rerenders_its_slot(terminal_status):
    app, bridge, widgets = _app_with_mock_widgets()
    timer = _FakeTimer()
    app._thinking_pulse_timer = timer
    app._pulse_thinking_marker()
    assert timer.pause.call_count == 1

    bridge.emit(_running_submission())
    app._drain_bridge_events()
    assert app._thinking_pulse_paused is False
    assert timer.reset.call_count == 1

    app._pulse_thinking_marker()
    app._pulse_thinking_marker()

    assert timer.pause.call_count == 1
    # A esfera e o elapsed vêm da re-renderização do próprio slot a cada pulso.
    index, item = app._feed_model.active_submission_items()[0]
    assert widgets["#feed"].update_entry.call_count == 2
    assert widgets["#feed"].update_entry.call_args.args[:2] == (index, id(item.event))
    assert app._feed_renderable_cache[id(item.event)] is widgets["#feed"].update_entry.call_args.args[2]

    bridge.emit(
        TextualUiEvent(
            "submission_status",
            {"submission_id": "s1", "status": terminal_status, "revision": 2},
        )
    )
    app._drain_bridge_events()
    assert app._feed_model.active_submission_items() == []

    app._pulse_thinking_marker()
    assert timer.pause.call_count == 2
    assert app._thinking_pulse_paused is True


def test_pulse_processes_mcp_expiry_before_pausing():
    app, bridge, widgets = _app_with_mock_widgets()
    now = [10.0]
    app._feed_model = TextualFeedModel(clock=lambda: now[0], tool_preview_dwell_seconds=0.5)
    timer = _FakeTimer()
    app._thinking_pulse_timer = timer
    common = {"session_id": "session-1", "client_name": "codexcloud", "transport": "mcp_http"}

    bridge.emit(
        TextualUiEvent(
            "tool_preview",
            {**common, "content": "⚒ grep_search", "mcp_msg_id": "1"},
            agent="codexcloud-gpt-6",
        )
    )
    bridge.emit(
        TextualUiEvent(
            "tool_state",
            {**common, "msg_id": "1", "tool_name": "grep_search", "status": "finished", "duration_ms": 750},
            agent="codexcloud-gpt-6",
        )
    )
    app._drain_bridge_events()
    assert app._feed_model.has_transients

    app._pulse_thinking_marker()
    assert timer.pause.call_count == 0

    now[0] = 10.6
    syncs_before = widgets["#feed"].sync_entries.call_count
    app._pulse_thinking_marker()
    assert not app._feed_model.has_transients
    assert widgets["#feed"].sync_entries.call_count == syncs_before + 1
    # O tick que expirou o burst não pausa: dá um ciclo de folga ao modelo.
    assert timer.pause.call_count == 0

    app._pulse_thinking_marker()
    assert timer.pause.call_count == 1
    assert app._thinking_pulse_paused is True


def test_pause_reset_failures_and_shutdown_do_not_corrupt_timer_state():
    app, _bridge, _widgets = _app_with_mock_widgets()
    timer = _FakeTimer()
    app._thinking_pulse_timer = timer
    timer.pause.side_effect = RuntimeError("encerrando")
    app._pause_thinking_pulse()
    assert app._thinking_pulse_paused is False
    timer.pause.side_effect = None
    app._pause_thinking_pulse()
    assert app._thinking_pulse_paused is True
    timer.reset.side_effect = RuntimeError("encerrando")
    app._resume_thinking_pulse()
    assert app._thinking_pulse_paused is True
    app._thinking_pulse_timer = None
    app._resume_thinking_pulse()
    assert app._thinking_pulse_paused is False


# --- App headless real --------------------------------------------------------


def _timer_is_active(timer) -> bool | None:
    active = getattr(timer, "_active", None)
    return active.is_set() if active is not None else None


@asynccontextmanager
async def _managed_headless_app(app, stopped):
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            yield pilot
        finally:
            # O worker precisa concluir call_from_thread(exit) enquanto o loop
            # ainda existe, não depois de asyncio.run fechar o loop do piloto.
            stopped.set()
            if app._worker_thread:
                await asyncio.to_thread(app._worker_thread.join, 0.5)


def test_headless_app_pauses_pulse_timer_and_leaves_hidden_status_alone():
    stopped = threading.Event()
    quimera_app = SimpleNamespace(renderer=None, input_gate=None, run=stopped.wait)
    bridge = TextualUiBridge()
    app = _capture_app(quimera_app, bridge)

    async def run_test() -> None:
        async with _managed_headless_app(app, stopped) as pilot:
            await pilot.pause()
            # Não depende de já terem passado 200 ms em um host rápido.
            app._poll_active_agent()
            app._pulse_thinking_marker()
            assert app._thinking_pulse_paused is True
            assert _timer_is_active(app._thinking_pulse_timer) in (False, None)

            agent_status = app.query_one("#agent_status")
            status_bar = app.query_one("#status_bar")
            assert agent_status.display is False
            assert status_bar.display is False
            assert app._blank_status_selectors == {"#status_bar", "#agent_status"}

            # A primeira escrita/hide legítima acima pode ter um layout
            # pendente. Mede idle só depois de o frame de montagem assentar.
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            counts = {"status_update": 0, "layout": 0, "pulse_ticks": 0}

            def _count(key, original):
                def wrapped(*args, **kwargs):
                    counts[key] += 1
                    return original(*args, **kwargs)

                return wrapped

            agent_status.update = _count("status_update", agent_status.update)
            status_bar.update = _count("status_update", status_bar.update)
            app.screen._refresh_layout = _count("layout", app.screen._refresh_layout)
            app._feed_model.expire_tool_previews = _count(
                "pulse_ticks", app._feed_model.expire_tool_previews
            )

            # Janela de idle: polls de 0,2 s continuam rodando, mas não
            # escrevem nos Statics nem pedem layout; o pulso está pausado.
            await pilot.pause(0.6)
            assert counts == {"status_update": 0, "layout": 0, "pulse_ticks": 0}

            # O produtor de worker só enfileira; a reconciliação retoma o timer.
            await asyncio.to_thread(
                bridge.emit,
                TextualUiEvent("agent_update", {"label": "claude", "content": "gerando..."}, agent="claude"),
            )
            app.flush_bridge_events()
            assert app._feed_model.has_transients
            assert app._thinking_pulse_paused is False
            assert _timer_is_active(app._thinking_pulse_timer) in (True, None)
            assert counts["status_update"] == 0
            bridge.emit(TextualUiEvent("agent_message", {"content": "fim"}, agent="claude"))
            app.flush_bridge_events()
            app._pulse_thinking_marker()
            assert app._thinking_pulse_paused is True

    try:
        asyncio.run(run_test())
    finally:
        stopped.set()
        if app._worker_thread:
            app._worker_thread.join(timeout=0.5)
