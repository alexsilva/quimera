"""Aplicação Textual principal do Quimera."""
from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path


def _is_android() -> bool:
    """Detecta Android para desabilitar mouse tracking no Textual.

    O Textual ativa ?1003h (any-event tracking) que faz o Termux rotear todos
    os toques como escape sequences — impedindo que o teclado virtual reabra ao
    tocar a tela. Sem mouse=False, showSoftInput() nunca é chamado pelo Termux.
    """
    try:
        return "android" in os.uname().release.lower()
    except Exception:
        return False


from quimera.app.config import handler as _screen_handler
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.constants import SUMMARY_SPINNER_FRAMES as _SUMMARY_SPINNER_FRAMES
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.renderables import (
    _build_question_overlay,
    _build_window_overlay_payload,
    _clear_question_overlay_widget,
    _render_event,
)
from quimera.ui.textual.terminal_modes import (
    _restore_terminal_modes,
    _restore_textual_input_focus,
)
from quimera.ui.textual.styles import TEXTUAL_APP_CSS


def _resolve_textual_feed_limit(quimera_app) -> int | None:
    """Retorna o limite visual do feed Textual.

    O feed é scrollback visual, não janela de contexto. Configurações como
    history_window e auto_summarize_threshold limitam memória/prompt, mas não
    podem truncar a saída rolável dos agentes.
    """
    return None


def _append_post_exit_failure_message(
    messages: list[tuple[str, str]],
    event: TextualUiEvent,
) -> bool:
    """Guarda falhas exibidas no alt-screen para reimpressão após a saída."""
    if event.kind not in {"error", "warning"}:
        return False
    content = str(event.payload or "").strip()
    if not content:
        return False
    messages.append((event.kind, content))
    return True


def run_textual_quimera_app(quimera_app, bridge: TextualUiBridge) -> None:
    """Executa a interface Textual como UI principal do Quimera."""
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Input, RichLog, Static
        from quimera.app.completion_dropdown import CompletionDropdown
        from quimera.ui.textual.widgets import _CompletionInput, _SummaryHeader, _SummarySpinner
    except ImportError as exc:
        raise SystemExit(
            "A interface Textual requer a dependência 'textual'. "
            "Reinstale com: pip install -e ."
        ) from exc

    _post_exit_messages: list[tuple[str, str]] = []

    class QuimeraTextualApp(App):
        """TUI principal do Quimera."""

        CSS = TEXTUAL_APP_CSS

        TITLE = "Quimera"

        BINDINGS = [
            ("ctrl+c", "cancel_or_exit", "Cancelar/Sair"),
            ("ctrl+q", "cancel_or_exit", "Sair"),
            ("ctrl+t", "cycle_theme", "Tema"),
            ("alt+t", "cycle_theme", "Tema"),
            ("f6", "cycle_theme", "Tema"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._worker_thread: threading.Thread | None = None
            self._commands: list[str] = []
            self._summarizing = False
            self._spinner_index = 0
            self._spinner_timer = None
            self._bridge_drain_timer = None
            self._feed_model = TextualFeedModel()
            self._history_file_path: Path | None = None

        def compose(self) -> ComposeResult:
            yield _SummaryHeader(show_clock=True, id="header")
            with Vertical(id="main"):
                yield RichLog(
                    id="feed",
                    markup=True,
                    wrap=True,
                    highlight=False,
                    max_lines=_resolve_textual_feed_limit(quimera_app),
                )
                yield Static("", id="toolbar")
                yield Static("", id="question_overlay")
                yield CompletionDropdown()
                with Horizontal(id="input_bar"):
                    yield _CompletionInput(id="input")

        def on_mount(self) -> None:
            bridge.attach_textual_app(self)
            bridge.set_input_value("")
            gate = getattr(quimera_app, "input_gate", None)
            if hasattr(gate, "set_textual_mounted"):
                gate.set_textual_mounted(True)
            for event in bridge.drain_pending_events():
                self.handle_bridge_event(event)
            self._bridge_drain_timer = self.set_interval(0.05, self._drain_bridge_events)
            self._worker_thread = threading.Thread(
                target=self._run_quimera_app,
                daemon=True,
                name="quimera-textual-loop",
            )
            self._worker_thread.start()
            input_widget = self.query_one("#input", _CompletionInput)
            history_file = getattr(gate, "_history_file", None)
            self._history_file_path = Path(history_file).expanduser() if history_file else None
            input_widget.load_history(self._history_file_path)
            input_widget.focus()

        def on_unmount(self) -> None:
            gate = getattr(quimera_app, "input_gate", None)
            if hasattr(gate, "set_textual_mounted"):
                gate.set_textual_mounted(False)
            if self._bridge_drain_timer is not None:
                try:
                    self._bridge_drain_timer.stop()
                except Exception:
                    pass
                self._bridge_drain_timer = None
            try:
                self.query_one("#input", _CompletionInput).save_history(self._history_file_path)
            except Exception:
                pass

        def _drain_bridge_events(self) -> None:
            drained = False
            for event in bridge.drain_pending_events():
                drained = True
                self.handle_bridge_event(event)
            if drained:
                self._refresh_now(layout=True)

        def flush_bridge_events(self) -> None:
            self._drain_bridge_events()
            self._refresh_now(layout=True)

        def _start_spinner(self) -> None:
            """Inicia animação de loading ao lado do relógio do header."""
            if self._spinner_timer is not None:
                return
            self._summarizing = True
            self._spinner_index = 0
            self._update_spinner()
            self._spinner_timer = self.set_interval(0.1, self._update_spinner)

        def _stop_spinner(self) -> None:
            """Para animação de loading e limpa o indicador do header."""
            self._summarizing = False
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self.query_one("#summary-spinner", _SummarySpinner).update("")

        def _update_spinner(self) -> None:
            """Avança o frame do spinner no header."""
            frame = _SUMMARY_SPINNER_FRAMES[self._spinner_index % len(_SUMMARY_SPINNER_FRAMES)]
            self.query_one("#summary-spinner", _SummarySpinner).update(frame)
            self._spinner_index += 1

        def _run_quimera_app(self) -> None:
            try:
                quimera_app.run()
            except Exception as exc:  # noqa: BLE001
                _post_exit_messages.append(
                    ("error", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                )
            finally:
                try:
                    self.call_from_thread(self.exit)
                except RuntimeError:
                    return

        def on_input_submitted(self, event: Input.Submitted) -> None:
            event.stop()
            self.query_one(CompletionDropdown).hide()
            value = event.input.user_value
            event.input.reset_to_prefix()
            bridge.set_input_value("")
            if value:
                event.input.add_to_history(value)
            bridge.submit_input(value)

        def _set_question_overlay(self, payload) -> None:
            overlay = self.query_one("#question_overlay", Static)
            overlay.update(_build_question_overlay(payload))
            overlay.display = True
            _restore_textual_input_focus(self)

        def _clear_question_overlay(self) -> None:
            overlay = self.query_one("#question_overlay", Static)
            _clear_question_overlay_widget(overlay)

        def _clear_prompt_state(self) -> None:
            gate = getattr(quimera_app, "input_gate", None)
            clearer = getattr(gate, "clear_interactive_prompt_state", None)
            if callable(clearer):
                clearer()
            self._refresh_toolbar()

        def _refresh_now(self, *, layout: bool = False) -> None:
            try:
                self.refresh(layout=layout)
            except Exception:
                return

        def _refresh_toolbar(self) -> None:
            gate = getattr(quimera_app, "input_gate", None)
            builder = getattr(gate, "_build_toolbar_renderable", None)
            if callable(builder):
                self.query_one("#toolbar", Static).update(builder())

        def action_cancel_or_exit(self) -> None:
            bridge.cancel_or_exit()

        def action_cycle_theme(self) -> None:
            gate = getattr(quimera_app, "input_gate", None)
            handler = getattr(gate, "_theme_cycle_handler", None)
            if callable(handler):
                handler()
            self._refresh_toolbar()

        def on_input_changed(self, event: Input.Changed) -> None:
            if not isinstance(event.input, _CompletionInput):
                return
            dropdown = self.query_one(CompletionDropdown)
            value = event.input.user_value if isinstance(event.input, _CompletionInput) else str(event.value)
            bridge.set_input_value(value)

            if not value:
                dropdown.hide()
                return
            if " " in value and not value.startswith(("/", "s/", "r/")):
                dropdown.hide()
                return
            if value.startswith(("/", "s/", "r/")):
                gate = getattr(quimera_app, "input_gate", None)
                if gate and callable(getattr(gate, "completions_for", None)):
                    extra = gate.completions_for(value)
                else:
                    extra = []
                dropdown.set_completions(extra)
                dropdown.filter(value)
            else:
                dropdown.set_completions([])
                dropdown.filter("")

        def handle_bridge_event(self, event: TextualUiEvent) -> None:
            _append_post_exit_failure_message(_post_exit_messages, event)
            if event.kind == "clear":
                self._feed_model.clear()
                self.query_one("#feed", RichLog).clear()
                self._refresh_now(layout=True)
                return
            if event.kind == "question":
                self._set_question_overlay(event.payload)
                self._refresh_toolbar()
                self._refresh_now(layout=True)
                return
            if event.kind == "window_open":
                self._set_question_overlay(_build_window_overlay_payload(event.payload))
                self._refresh_toolbar()
                self._refresh_now(layout=True)
                return
            if event.kind == "question_clear":
                self._clear_question_overlay()
                self._clear_prompt_state()
                self._refresh_now(layout=True)
                return
            if event.kind == "window_clear":
                self._clear_question_overlay()
                self._refresh_toolbar()
                self._refresh_now(layout=True)
                return
            if event.kind == "prompt_clear":
                self._clear_prompt_state()
                self._refresh_now(layout=True)
                return
            if event.kind == "theme_changed":
                self._refresh_toolbar()
                self._refresh_now()
                return
            if event.kind == "summarizing":
                if event.payload:
                    self._start_spinner()
                else:
                    self._stop_spinner()
                self._refresh_now()
                return
            if event.kind == "prompt":
                payload = event.payload or {}
                toolbar = payload.get("toolbar", "")
                self._commands = list(payload.get("commands", []) or [])
                self.query_one("#toolbar", Static).update(toolbar)
                self.query_one("#input", Input).focus()
                self._refresh_now(layout=True)
                return
            if not self._feed_model.apply(event):
                return
            feed = self.query_one("#feed", RichLog)
            change = self._feed_model.last_change
            if change.redraw:
                feed.clear()
                for item in self._feed_model.items:
                    renderable = _render_event(item.event)
                    if renderable is not None:
                        feed.write(renderable)
                self._refresh_toolbar()
                self._refresh_now(layout=True)
                return
            if change.appended is not None:
                renderable = _render_event(change.appended.event)
                if renderable is not None:
                    feed.write(renderable)
            self._refresh_toolbar()
            self._refresh_now()

    bridge.attach_quimera_app(quimera_app)
    renderer = getattr(quimera_app, "renderer", None)
    if renderer is not None and hasattr(renderer, "set_profile_resolver"):
        renderer.set_profile_resolver(quimera_app._resolve_profile_style)
    try:
        QuimeraTextualApp().run(mouse=not _is_android())
    finally:
        _restore_terminal_modes()

    # Após o Textual sair (tela alternativa restaurada), drena eventos pendentes
    # e imprime erros/warnings para a tela normal — assim não desaparecem.
    _screen_handler.drain_to_stderr()
    for _ev in bridge.drain_pending_events():
        _append_post_exit_failure_message(_post_exit_messages, _ev)
    for _kind, _content in _post_exit_messages:
        if _content:
            print(_content, file=sys.stderr, flush=True)
