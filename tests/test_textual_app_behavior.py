"""Comportamento do QuimeraTextualApp real dirigido por Pilot.

Substitui as asserções sobre o código-fonte do app (inspect.getsource) por
efeitos observáveis: tema, bridge, pulso, âncora do feed, overlay de
pergunta, notificações e o toggle das ferramentas de turnos encerrados.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from quimera.ui.textual import app as textual_app_module
from quimera.ui.textual.app import QuimeraTextualApp, run_textual_quimera_app
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.constants import (
    SUMMARY_NOTIFICATION_MESSAGE,
    SUMMARY_SPINNER_FRAMES,
)
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.widgets import _SummarySpinner, _UnifiedFeed


def _capture_app(quimera_app, bridge: TextualUiBridge) -> QuimeraTextualApp:
    """Constrói o app real pelo ponto de entrada, sem entrar no terminal."""
    captured: list[QuimeraTextualApp] = []
    with patch("textual.app.App.run", lambda self, *args, **kwargs: captured.append(self)):
        run_textual_quimera_app(quimera_app, bridge)
    return captured[0]


def _fake_quimera_app(**attributes) -> SimpleNamespace:
    # run() bloqueia: se retornasse, o worker encerraria a UI no meio do teste.
    attributes.setdefault("input_gate", None)
    return SimpleNamespace(renderer=None, run=threading.Event().wait, **attributes)


def _app(**attributes) -> tuple[QuimeraTextualApp, TextualUiBridge]:
    bridge = TextualUiBridge()
    return _capture_app(_fake_quimera_app(**attributes), bridge), bridge


def _final(agent: str, content: str, **payload) -> TextualUiEvent:
    return TextualUiEvent("agent_message", {"content": content, **payload}, agent=agent)


def _update(agent: str, content: str, **payload) -> TextualUiEvent:
    return TextualUiEvent("agent_update", {"content": content, **payload}, agent=agent)


def _preview(agent: str, content: str, **payload) -> TextualUiEvent:
    return TextualUiEvent("tool_preview", {"content": content, **payload}, agent=agent)


def _emit_from_worker(bridge: TextualUiBridge, event: TextualUiEvent) -> None:
    """Emite como um agente faria: de outra thread, só enfileirando."""
    worker = threading.Thread(target=bridge.emit, args=(event,))
    worker.start()
    worker.join()


def _slot_text(app: QuimeraTextualApp, seq: int) -> str:
    console = Console(file=StringIO(), width=120, color_system=None)
    console.print(app._feed_renderable_cache[seq])
    return console.file.getvalue()


def _fill_history(bridge: TextualUiBridge, turns: int) -> None:
    for index in range(turns):
        bridge.emit(_final("claude", f"resposta {index}\nlinha 2\nlinha 3"))


def _completed_turn_with_tools(bridge: TextualUiBridge, run_id: str, tools: int) -> None:
    bridge.emit(_update("codex", "trabalhando", run_id=run_id))
    for index in range(tools):
        bridge.emit(_preview("codex", f"⚒ read_file {run_id}_{index}.py", run_id=run_id))
    bridge.emit(_final("codex", f"pronto {run_id}", run_id=run_id))


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.pause()


def test_theme_cycle_keys_call_the_theme_handler():
    calls: list[str] = []
    gate = SimpleNamespace(_theme_cycle_handler=lambda: calls.append("ciclo"))
    app, _ = _app(input_gate=gate)

    async def run() -> None:
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            for key in ("ctrl+t", "alt+t", "f6"):
                await pilot.press(key)
            await pilot.pause()

    asyncio.run(run())

    assert calls == ["ciclo", "ciclo", "ciclo"]


def test_bridge_event_updates_feed_and_render_failure_does_not_kill_app(monkeypatch, caplog):
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            bridge.emit(_final("claude", "primeira resposta"))
            await pilot.pause()
            seq = app._feed_model.items[-1].seq
            feed = app.query_one("#feed", _UnifiedFeed)
            assert feed._entry_tokens[-1] == seq
            assert "primeira resposta" in _slot_text(app, seq)

            def explode(event):
                raise RuntimeError("render quebrado")

            monkeypatch.setattr(textual_app_module, "_render_event", explode)
            with caplog.at_level(logging.ERROR, logger=textual_app_module._logger.name):
                bridge.emit(_final("claude", "segunda resposta"))
                await pilot.pause()

            assert app.is_running
            assert any("render quebrado" in str(record.exc_info) for record in caplog.records)

            monkeypatch.undo()
            bridge.emit(_final("claude", "terceira resposta"))
            await pilot.pause()
            assert "terceira resposta" in _slot_text(app, app._feed_model.items[-1].seq)

    asyncio.run(run())


def test_pulse_removes_expired_preview_slot_when_nothing_else_is_transient():
    now = [100.0]
    app, bridge = _app()
    app._feed_model = TextualFeedModel(clock=lambda: now[0], tool_preview_dwell_seconds=0.5)
    common = {"session_id": "s1", "client_name": "claude-code", "transport": "mcp_http"}

    async def run() -> None:
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            bridge.emit(_preview("claude", "⚒ grep_search", mcp_msg_id="1", **common))
            bridge.emit(
                TextualUiEvent(
                    "tool_state",
                    {**common, "msg_id": "1", "tool_name": "grep_search", "status": "finished"},
                    agent="claude",
                )
            )
            await pilot.pause()
            assert len(feed._entry_tokens) == 1

            now[0] += 1.0
            app._pulse_thinking_marker()
            await pilot.pause()

            assert app._feed_model.items == []
            assert feed._entry_tokens == []

    asyncio.run(run())


def test_anchored_feed_follows_transient_growth_and_scrolled_feed_stays_put():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 20)
            await _settle(pilot)
            assert feed.scroll_y == feed.max_scroll_y > 0

            thought = "pensando"
            for _ in range(3):
                thought += "\nmais uma linha de raciocínio"
                bridge.emit(_update("claude", thought, run_id="r1"))
                await pilot.pause()
                assert feed.scroll_y == feed.max_scroll_y

            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            assert feed.scroll_y == 0
            for _ in range(3):
                thought += "\nainda mais raciocínio"
                bridge.emit(_update("claude", thought, run_id="r1"))
                await pilot.pause()
                assert feed.scroll_y == 0

    asyncio.run(run())


def test_prompt_submission_scrolls_to_end_once():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 20)
            await _settle(pilot)
            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            assert feed.scroll_y == 0

            await pilot.press("o", "i", "enter")
            await _settle(pilot)
            assert feed.scroll_y == feed.max_scroll_y > 0
            assert app._scroll_feed_on_submit is False

            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            bridge.emit(_final("claude", "resposta depois do envio"))
            await _settle(pilot)
            assert feed.scroll_y == 0

    asyncio.run(run())


def test_control_command_submission_does_not_leave_scroll_flag_armed():
    # /agentes sem argumento é controle: não espelha turno humano no feed,
    # então nenhum sync consome a flag no envio. Ela não pode sobrar.
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 20)
            await _settle(pilot)
            before = len(app._feed_model.items)
            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            assert feed.scroll_y == 0

            await pilot.press("/", "a", "g", "e", "n", "t", "e", "s", "enter")
            await _settle(pilot)
            assert len(app._feed_model.items) == before
            assert bridge.input_queue.get_nowait() == "/agentes"
            # O envio reengata a âncora na hora, mesmo sem item novo no feed.
            assert feed.scroll_y == feed.max_scroll_y > 0
            assert app._scroll_feed_on_submit is False

            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            bridge.emit(_final("claude", "resposta depois do comando"))
            await _settle(pilot)
            assert feed.scroll_y == 0

    asyncio.run(run())


def test_question_answer_submission_does_not_leave_scroll_flag_armed():
    # A resposta a uma pergunta vai à fila direta e só limpa o overlay; o
    # feed não muda e a flag não pode puxar o scroll na próxima resposta.
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 20)
            await _settle(pilot)
            before = len(app._feed_model.items)
            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            bridge.begin_direct_input()
            try:
                await pilot.press("s", "i", "m", "enter")
                await _settle(pilot)
            finally:
                bridge.end_direct_input()
            assert bridge.direct_input_queue.get_nowait() == "sim"
            assert len(app._feed_model.items) == before
            assert feed.scroll_y == feed.max_scroll_y > 0
            assert app._scroll_feed_on_submit is False

            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            bridge.emit(_final("claude", "resposta depois da pergunta"))
            await _settle(pilot)
            assert feed.scroll_y == 0

    asyncio.run(run())


def test_pulse_updates_only_transient_slots(monkeypatch):
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 3)
            bridge.emit(_update("claude", "pensando", run_id="r1"))
            await pilot.pause()

            synced: list[object] = []
            updated: list[int] = []
            original_update = feed.update_entry
            monkeypatch.setattr(feed, "sync_entries", lambda *args, **kwargs: synced.append(args))

            def spy_update(index, token, renderable):
                updated.append(token)
                return original_update(index, token, renderable)

            monkeypatch.setattr(feed, "update_entry", spy_update)
            app._pulse_thinking_marker()

            transient_seqs = [item.seq for _, item in app._feed_model.transient_items()]
            assert transient_seqs
            assert updated == transient_seqs
            assert synced == []

    asyncio.run(run())


def test_event_emitted_from_worker_is_applied_by_the_drain_timer():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            _emit_from_worker(bridge, _final("claude", "vinda do worker"))
            assert bridge.pending_event_count() == 1
            assert app._feed_model.items == []

            # O timer de drenagem é de 50 ms; num host estrangulado uma pausa
            # fixa pode não bastar. Espera o evento sair da fila, com teto.
            for _ in range(100):
                if bridge.pending_event_count() == 0:
                    break
                await pilot.pause(0.05)

            assert bridge.pending_event_count() == 0
            assert app._feed_model.items[-1].event.payload["content"] == "vinda do worker"

    asyncio.run(run())


def test_timer_callbacks_are_inert_after_the_app_has_exited():
    # Os timers param no unmount, mas um tick já despachado ainda pode rodar
    # com a tela desmontada. Nenhum caminho pode levantar NoMatches, e o
    # evento enfileirado sobrevive para a drenagem pós-run.
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            bridge.emit(_update("claude", "pensando", run_id="r1"))
            await pilot.pause()

        # Janela real do encerramento: exit() já sinalizou e a tela se foi.
        app._exit = True
        _emit_from_worker(bridge, _final("claude", "tarde demais"))
        app._sync_feed()
        app._sync_transient_feed_slots()
        app._sync_submission_feed_slots([])
        app._pulse_thinking_marker()
        app._drain_bridge_events()
        assert bridge.pending_event_count() == 1

        # O ramo NoMatches em si: tela desmontada, sem a flag de saída.
        app._exit = False
        assert app._feed_widget() is None
        app._sync_feed()
        app._sync_transient_feed_slots()
        app._sync_submission_feed_slots([])

    asyncio.run(run())


def test_flush_bridge_events_applies_pending_preview_synchronously():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            _emit_from_worker(bridge, _preview("claude", "⚒ list_files", run_id="r1"))
            assert not app._feed_model.has_transients

            app.flush_bridge_events()

            assert bridge.pending_event_count() == 0
            assert app._feed_model.has_transients
            feed = app.query_one("#feed", _UnifiedFeed)
            assert feed._entry_tokens == [item.seq for item in app._feed_model.items]

    asyncio.run(run())


def test_tool_preview_is_tracked_for_status_until_final_message():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            bridge.emit(_preview("claude", "⚒ read_file a.py", run_id="r1"))
            await pilot.pause()
            assert app._active_tool_previews == {"claude": "⚒ read_file a.py"}
            assert app.active_agent == "claude"

            bridge.emit(_final("claude", "pronto", run_id="r1"))
            await pilot.pause()
            assert app._active_tool_previews == {}
            assert app.active_agent is None

    asyncio.run(run())


def test_question_overlay_shows_and_answer_clears_overlay_and_prompt():
    bridge = TextualUiBridge()
    gate = bridge.create_input_gate()
    app = _capture_app(_fake_quimera_app(input_gate=gate), bridge)
    answers: list[object] = []

    async def run() -> None:
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            overlay = app.query_one("#question_overlay")
            assert overlay.display is False
            worker = threading.Thread(
                target=lambda: answers.append(
                    gate.read_selection_in_terminal("Qual caminho?", ["norte", "sul"], timeout=10)
                )
            )
            worker.start()
            for _ in range(100):
                await pilot.pause(0.02)
                if overlay.display:
                    break
            assert overlay.display is True

            await pilot.press("2", "enter")
            for _ in range(100):
                await pilot.pause(0.02)
                if not worker.is_alive() and not overlay.display:
                    break
            worker.join(timeout=1)

            assert overlay.display is False
            assert gate._interactive_prompt_active is False
            assert app.query_one("#input").submission_value == ""

    asyncio.run(run())

    assert answers == [(1, "sul")]


def test_summarizing_shows_and_clears_summary_spinner():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            spinner = app.query_one("#summary-spinner", _SummarySpinner)
            bridge.emit(TextualUiEvent("summarizing", True))
            await pilot.pause()
            assert str(spinner.render()) in SUMMARY_SPINNER_FRAMES

            bridge.emit(TextualUiEvent("summarizing", False))
            await pilot.pause()
            assert str(spinner.render()) == ""

    asyncio.run(run())


def test_notification_and_summarizing_events_call_notify(monkeypatch):
    app, bridge = _app()
    notified: list[tuple[str, dict]] = []

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(app, "notify", lambda message, **kwargs: notified.append((message, kwargs)))
            bridge.emit(TextualUiEvent("notification", {"message": "salvo", "severity": "warning"}))
            bridge.emit(TextualUiEvent("summarizing", True))
            await pilot.pause()
            bridge.emit(TextualUiEvent("summarizing", False))

    asyncio.run(run())

    assert notified[0] == ("salvo", {"severity": "warning", "timeout": None, "markup": False})
    assert SUMMARY_NOTIFICATION_MESSAGE == "Gerando resumo"
    assert notified[1] == (
        f"{SUMMARY_SPINNER_FRAMES[0]} Gerando resumo",
        {"severity": "information", "timeout": 4, "markup": False},
    )
    assert len(notified) == 2


def test_toggle_turn_tools_grows_the_slot_without_moving_the_scrolled_feed():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 4)
            _completed_turn_with_tools(bridge, "r1", tools=6)
            _fill_history(bridge, 12)
            await _settle(pilot)
            seq = app._feed_model.latest_toggleable_seq()
            assert seq is not None
            index = feed._entry_tokens.index(seq)
            slot = feed._entry_widgets[index]
            collapsed_height = slot.region.height
            assert feed.is_anchored
            scroll_before = feed.scroll_y
            assert scroll_before == feed.max_scroll_y > 0

            app.action_toggle_turn_tools(seq)
            await _settle(pilot)

            assert "read_file r1_5.py" in _slot_text(app, seq)
            assert slot.region.height == collapsed_height + 6
            # Ancorado, o crescimento de um turno do meio puxaria o fim; a
            # âncora é solta para o leitor não perder o turno clicado.
            assert feed.scroll_y == scroll_before
            assert feed.scroll_y < feed.max_scroll_y

            app.action_toggle_turn_tools(seq)
            await _settle(pilot)
            assert slot.region.height == collapsed_height
            assert "read_file r1_5.py" not in _slot_text(app, seq)

    asyncio.run(run())


def test_f4_reveals_the_latest_completed_turn_when_reading_history():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 12)
            _completed_turn_with_tools(bridge, "r1", tools=6)
            _fill_history(bridge, 12)
            await _settle(pilot)
            seq = app._feed_model.latest_toggleable_seq()
            assert seq is not None
            slot = feed._entry_widgets[feed._entry_tokens.index(seq)]
            feed.scroll_to(y=0, animate=False)
            await _settle(pilot)
            assert feed.scroll_y == 0
            # Lendo o topo do histórico, o turno com tools está fora da tela.
            assert slot.region.y >= feed.container_size.height

            await pilot.press("f4")
            await _settle(pilot)

            assert "read_file r1_5.py" in _slot_text(app, seq)
            assert feed.scroll_y > 0
            assert 0 <= slot.region.y < feed.container_size.height
            assert feed.scroll_y < feed.max_scroll_y

    asyncio.run(run())


def test_f4_reveals_the_top_of_the_last_turn_taller_than_the_viewport():
    # Caso mais comum do f4: o turno é o último item e o feed está ancorado.
    # Com mais tools do que cabe na tela, o reveal tem de mostrar o cabeçalho
    # do turno, não só a cauda das tools coladas no fim.
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            feed = app.query_one("#feed", _UnifiedFeed)
            _fill_history(bridge, 12)
            _completed_turn_with_tools(bridge, "r1", tools=40)
            await _settle(pilot)
            seq = app._feed_model.latest_toggleable_seq()
            assert seq == app._feed_model.items[-1].seq
            slot = feed._entry_widgets[feed._entry_tokens.index(seq)]
            assert feed.scroll_y == feed.max_scroll_y > 0

            await pilot.press("f4")
            await _settle(pilot)

            assert "read_file r1_39.py" in _slot_text(app, seq)
            assert slot.region.height > feed.container_size.height
            top = feed.content_region.y
            assert slot.region.y == top
            assert feed.scroll_y < feed.max_scroll_y

            # Quem pediu para ver o turno está lendo: um evento novo não
            # puxa o scroll de volta ao fim.
            bridge.emit(_update("claude", "nova execução\nlinha 2\nlinha 3", run_id="r9"))
            await _settle(pilot)
            assert slot.region.y == top

    asyncio.run(run())


def test_ctrl_o_toggles_the_latest_completed_turn():
    app, bridge = _app()

    async def run() -> None:
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            _completed_turn_with_tools(bridge, "r1", tools=1)
            _completed_turn_with_tools(bridge, "r2", tools=2)
            bridge.emit(_final("claude", "sem tools"))
            await pilot.pause()
            latest = app._feed_model.latest_toggleable_seq()
            assert latest is not None

            def expanded() -> dict[int, bool]:
                return {
                    item.seq: bool(item.event.payload.get("tools_expanded"))
                    for item in app._feed_model.items
                    if isinstance(item.event.payload, dict) and "tool_lines" in item.event.payload
                }

            await pilot.press("ctrl+o")
            await pilot.pause()
            assert [seq for seq, value in expanded().items() if value] == [latest]
            assert "read_file r2_1.py" in _slot_text(app, latest)

            await pilot.press("f4")
            await pilot.pause()
            assert not any(expanded().values())

    asyncio.run(run())
