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
from rich.console import Group as _RichGroup

from rich.text import Text as _RichText
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
        from quimera.ui.textual.widgets import _BreadcrumbWidget, _CompletionInput, _SummaryHeader, _SummarySpinner
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
            ("ctrl+l", "clear_feed", "Limpar feed"),
            ("ctrl+end", "scroll_to_bottom", "Ir ao fim"),
            ("ctrl+home", "scroll_to_top", "Ir ao topo"),
            ("pageup", "feed_page_up", "Rolar feed acima"),
            ("pagedown", "feed_page_down", "Rolar feed abaixo"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._worker_thread: threading.Thread | None = None
            self._commands: list[str] = []
            self._summarizing = False
            self._spinner_index = 0
            self._spinner_timer = None
            self._bridge_drain_timer = None
            self._active_agent_timer = None
            self.active_agent: str | None = None
            self._last_active_agent_info: tuple[str, str] | None = None
            self._active_tool_previews: dict[str, str] = {}
            self._last_status_bar_state: tuple[tuple[str, str] | None, tuple[tuple[str, str], ...]] | None = None
            self._feed_model = TextualFeedModel()
            self._history_file_path: Path | None = None
            self._feed_pinned_to_bottom = True
            self._restored_history_hydrated = False
            self._breadcrumb_chain: list[str] = []
            # Tracks event object ids already written to the RichLog (permanent items only).
            # Avoids clear+rewrite: we append-only and skip already-written items.
            self._written_to_richlog: set[int] = set()

        def compose(self) -> ComposeResult:
            yield _SummaryHeader(show_clock=True, id="header")
            with Vertical(id="main"):
                yield RichLog(
                    id="feed",
                    markup=True,
                    wrap=True,
                    highlight=False,
                    max_lines=_resolve_textual_feed_limit(quimera_app),
                    min_width=20,
                    auto_scroll=False,
                )
                yield Static("", id="feed_transient")
                yield Static("", id="toolbar")
                yield Static("", id="status_bar")
                yield Static("", id="question_overlay")
                yield CompletionDropdown()
            yield Static("", id="agent_status")
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
            self._active_agent_timer = self.set_interval(0.2, self._poll_active_agent)
            renderer = getattr(quimera_app, "renderer", None)
            if renderer is not None and hasattr(renderer, "set_orchestrator"):
                _orq = getattr(getattr(quimera_app, "agent_pool", None), "orchestrator_agent", None)
                if _orq:
                    renderer.set_orchestrator(_orq)
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
            if self._active_agent_timer is not None:
                try:
                    self._active_agent_timer.stop()
                except Exception:
                    pass
                self._active_agent_timer = None
            try:
                self.query_one("#input", _CompletionInput).save_history(self._history_file_path)
            except Exception:
                pass

        def _hydrate_restored_history(self) -> bool:
            """Carrega o histórico já restaurado pelo core no feed visual."""
            if self._restored_history_hydrated:
                return False
            history = getattr(quimera_app, "history", None)
            if not history:
                return False
            self._restored_history_hydrated = True
            resolver = getattr(quimera_app, "_resolve_profile_style", None)
            return self._feed_model.hydrate_from_history(
                list(history),
                user_label=str(getattr(quimera_app, "user_name", ">>>") or ">>>"),
                agent_resolver=resolver if callable(resolver) else None,
            )

        def _status_preview_text(self, value) -> str:
            line = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
            if len(line) > 96:
                return f"{line[:93]}..."
            return line

        def _record_status_event(self, event: TextualUiEvent) -> None:
            if event.kind == "tool_preview":
                preview = self._status_preview_text(event.payload)
                if preview:
                    self._active_tool_previews[str(event.agent or "tool")] = preview
                if event.agent:
                    self.active_agent = event.agent
                return
            if event.kind == "delegation":
                payload = event.payload if isinstance(event.payload, dict) else {}
                chain = [str(item).strip() for item in (payload.get("chain") or []) if str(item).strip()]
                from_label = str(payload.get("from_label", "agente"))
                to_label = str(payload.get("to_label", "agente"))
                breadcrumb_items = [from_label, to_label]
                if chain:
                    breadcrumb_items = [*chain, to_label]
                self._breadcrumb_chain = breadcrumb_items
                self._update_breadcrumb()
                if event.agent:
                    self.active_agent = event.agent
                return
            if event.kind in {"stream_start", "stream_chunk", "agent_update"} and event.agent:
                self.active_agent = event.agent
                return
            if event.kind in {"agent_message", "stream_abort"} and event.agent:
                self._active_tool_previews.pop(str(event.agent), None)
                if self.active_agent == event.agent:
                    self.active_agent = None
                return
            if event.kind == "visual_reset":
                if event.agent:
                    self._active_tool_previews.pop(str(event.agent), None)
                    if self.active_agent == event.agent:
                        self.active_agent = None
                else:
                    self._active_tool_previews.clear()
                    self.active_agent = None
                return
            if event.kind == "agent_lifecycle" and event.agent:
                payload = event.payload if isinstance(event.payload, dict) else {}
                status = str(payload.get("status", "")).lower()
                if status in {"completed", "failed", "error", "cancelled", "aborted"}:
                    self._active_tool_previews.pop(str(event.agent), None)
                    if self.active_agent == event.agent:
                        self.active_agent = None

        def _update_agent_status_widget(self) -> None:
            agent_status = self.query_one("#agent_status", Static)
            if self.active_agent:
                agent_status.display = True
                agent_status.update(f"[bold][{self.active_agent}][/bold] ▸ processing...")
            else:
                agent_status.display = False
                agent_status.update("")

        def _update_breadcrumb(self) -> None:
            chain = self._breadcrumb_chain
            if chain:
                breadcrumb_text = " > ".join(chain[:5])
                self.query_one("#breadcrumb", _BreadcrumbWidget).update(f"  {breadcrumb_text}")
            else:
                self.query_one("#breadcrumb", _BreadcrumbWidget).update("")

        def _clear_status_bar(self) -> None:
            self._active_tool_previews.clear()
            self._last_active_agent_info = None
            self._last_status_bar_state = None
            self._breadcrumb_chain = []
            self._update_breadcrumb()
            widget = self.query_one("#status_bar", Static)
            widget.display = False
            widget.update("")
            self.active_agent = None
            self._update_agent_status_widget()

        def _update_status_bar(self) -> None:
            self._update_agent_status_widget()
            info = bridge.active_agent_info()
            tools = tuple(self._active_tool_previews.items())
            state = (info, tools)
            if state == self._last_status_bar_state:
                return
            self._last_status_bar_state = state
            self._last_active_agent_info = info
            widget = self.query_one("#status_bar", Static)
            if info is None and not tools:
                widget.display = False
                widget.update("")
                return
            widget.display = True
            text = _RichText()
            text.append("[spy]", style="bold green")
            text.append(" ", style="dim")
            if tools:
                latest_tools = list(tools)[-2:]
                tool_parts = []
                for agent_name, preview in latest_tools:
                    tool_text = _RichText()
                    tool_text.append(f"{agent_name} ▸ ", style="bold")
                    tool_text.append(preview, style="dim yellow")
                    tool_parts.append(tool_text)
                for i, part in enumerate(tool_parts):
                    if i > 0:
                        text.append(" | ", style="dim")
                    text.append(part)
            elif info is not None:
                label, style = info
                text.append(f"[{label}]", style=f"bold {style}")
                text.append(" ▸ ", style="dim")
                text.append("processando...", style="dim")
            widget.update(text)

        def _poll_active_agent(self) -> None:
            self._update_status_bar()

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

        def action_clear_feed(self) -> None:
            self._feed_model.clear()
            self.query_one("#feed", RichLog).clear()
            self._written_to_richlog.clear()
            self.query_one("#feed_transient", Static).update("")
            self._clear_status_bar()
            self._refresh_now(layout=True)

        def action_scroll_to_bottom(self) -> None:
            feed = self.query_one("#feed", RichLog)
            feed.scroll_end(animate=False)
            self._feed_pinned_to_bottom = True

        def action_scroll_to_top(self) -> None:
            feed = self.query_one("#feed", RichLog)
            feed.scroll_home(animate=False)
            self._feed_pinned_to_bottom = False

        def action_feed_page_up(self) -> None:
            feed = self.query_one("#feed", RichLog)
            feed.scroll_page_up(animate=False)
            self._feed_pinned_to_bottom = feed.is_vertical_scroll_end

        def action_feed_page_down(self) -> None:
            feed = self.query_one("#feed", RichLog)
            feed.scroll_page_down(animate=False)
            self._feed_pinned_to_bottom = feed.is_vertical_scroll_end

        def _feed_write(self, feed: RichLog, renderable) -> None:
            """Escreve no feed e rola para o fim apenas se estava ancorado ao fundo."""
            was_pinned = self._feed_pinned_to_bottom
            feed.write(renderable)
            if was_pinned:
                feed.scroll_end(animate=False)

        def _sync_transient_layer(self) -> None:
            """Atualiza o Static de transitórios sem tocar no RichLog."""
            try:
                widget = self.query_one("#feed_transient", Static)
            except Exception:
                return
            parts = []
            for item in self._feed_model.items:
                if item.transient:
                    r = _render_event(item.event)
                    if r is not None:
                        parts.append(r)
            if parts:
                widget.update(_RichGroup(*parts))
            else:
                widget.update("")

        def _sync_permanent_to_richlog(self, feed: RichLog) -> None:
            """Adiciona ao RichLog apenas itens permanentes ainda não escritos."""
            for item in self._feed_model.items:
                if item.transient:
                    continue
                key = id(item.event)
                if key not in self._written_to_richlog:
                    renderable = _render_event(item.event)
                    if renderable is not None:
                        self._feed_write(feed, renderable)
                    self._written_to_richlog.add(key)

        def _redraw_feed(self, feed: RichLog | None = None, *, scroll_end: bool = False) -> None:
            """Reescreve o feed completo — usado apenas na restauração de histórico."""
            feed = feed or self.query_one("#feed", RichLog)
            feed.clear()
            self._written_to_richlog.clear()
            for item in self._feed_model.items:
                if item.transient:
                    continue
                renderable = _render_event(item.event)
                if renderable is not None:
                    feed.write(renderable)
                    self._written_to_richlog.add(id(item.event))
            self._sync_transient_layer()
            if scroll_end:
                feed.scroll_end(animate=False)
            self._refresh_now(layout=True)

        def on_input_changed(self, event: Input.Changed) -> None:
            if not isinstance(event.input, _CompletionInput):
                return
            dropdown = self.query_one(CompletionDropdown)
            value = event.input.user_value if isinstance(event.input, _CompletionInput) else str(event.value)
            bridge.set_input_value(value)

            if not value:
                dropdown.hide()
                return
            if " " in value and not value.startswith(("/", "s/", "r/", "o/")):
                dropdown.hide()
                return
            if value.startswith(("/", "s/", "r/", "o/")):
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
            self._record_status_event(event)
            self._update_status_bar()
            if event.kind == "clear":
                self._feed_model.clear()
                self.query_one("#feed", RichLog).clear()
                self._written_to_richlog.clear()
                self.query_one("#feed_transient", Static).update("")
                self._clear_status_bar()
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
            if event.kind == "restore_history":
                if self._hydrate_restored_history():
                    self._redraw_feed(scroll_end=True)
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
                self._clear_status_bar()
                self._refresh_now(layout=True)
                return
            if not self._feed_model.apply(event):
                return
            feed = self.query_one("#feed", RichLog)
            change = self._feed_model.last_change
            if change.redraw:
                # A transient was updated in-place, or became permanent.
                # Append-only sync to RichLog (no clear) + update transient layer.
                self._sync_permanent_to_richlog(feed)
                self._sync_transient_layer()
                self._refresh_toolbar()
                self._refresh_now()
                return
            if change.appended is not None:
                if change.appended.transient:
                    # New transient slot — show in transient layer only.
                    self._sync_transient_layer()
                else:
                    # New permanent item — append to RichLog.
                    renderable = _render_event(change.appended.event)
                    if renderable is not None:
                        self._feed_write(feed, renderable)
                    self._written_to_richlog.add(id(change.appended.event))
                    self._sync_transient_layer()
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
