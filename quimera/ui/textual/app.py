"""Aplicação Textual principal do Quimera."""
# ruff: noqa: E402
from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import ClassVar

from quimera.clipboard_support import ClipboardManager


def _is_android() -> bool:
    """Detecta Android para desabilitar mouse tracking no Textual.

    O Textual ativa ?1003h (any-event tracking) que faz o Termux rotear todos
    os toques como escape sequences — impedindo que o teclado virtual reabra ao
    tocar a tela. Sem mouse=False, showSoftInput() nunca é chamado pelo Termux.
    """
    try:
        return "android" in os.uname().release.lower()
    except (AttributeError, OSError):
        return False


from quimera.app.config import handler as _screen_handler
from quimera.domain.prompt_formatter import PromptFormatter
from quimera.ui.browser import open_in_browser
from quimera.ui.textual.bridge import TextualUiBridge
from quimera.ui.textual.constants import (
    SUMMARY_NOTIFICATION_MESSAGE as _SUMMARY_NOTIFICATION_MESSAGE,
    SUMMARY_SPINNER_FRAMES as _SUMMARY_SPINNER_FRAMES,
)
from quimera.ui.textual.events import TextualUiEvent
from quimera.ui.textual.feed_model import TextualFeedModel
from quimera.ui.textual.theme_store import TuiThemeStore

_logger = logging.getLogger(__name__)

from quimera.ui.textual.driver import QuimeraLinuxDriver
from quimera.ui.textual.screenshot import feed_geometry
from quimera.ui.textual.renderables import (
    _build_question_overlay,
    _build_window_overlay_payload,
    _clear_question_overlay_widget,
    _render_event,
    advance_thinking_pulse,
    reset_thinking_pulse,
)
from quimera.ui.textual.terminal_modes import (
    _restore_terminal_modes,
    _restore_textual_input_focus,
)
from quimera.ui.textual.styles import TEXTUAL_APP_CSS
from collections.abc import Iterable

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Input, Static

from quimera.app.completion_dropdown import CompletionDropdown
from quimera.ui.textual.config_screen import ConfigScreen
from quimera.ui.textual.connection_screen import ConnectionScreen
from quimera.ui.textual.mcp_screen import MCPConnectionsScreen
from quimera.ui.textual.prompt_preview_screen import PromptPreviewScreen
from quimera.ui.textual.widgets import (
    _BreadcrumbWidget,
    _CompletionInput,
    _SummaryHeader,
    _SummarySpinner,
    _UnifiedFeed,
)


def _resolve_textual_feed_limit(quimera_app) -> int | None:
    """Mantém scrollback visual independente dos limites do contexto."""
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


def _read_clipboard_for_input(temp_image_dir: str | Path | None = None) -> str | None:
    """Lê texto ou imagem do clipboard e devolve payload inserível no input."""
    payload = ClipboardManager(temp_image_dir=temp_image_dir).read()
    if payload is None:
        return None
    return payload.text


def _clipboard_dir_for_app(quimera_app) -> Path | None:
    """Resolve o diretório temporário de anexos da sessão atual."""
    session_paths = getattr(quimera_app, "session_paths", None)
    clipboard_dir = getattr(session_paths, "clipboard_dir", None)
    return Path(clipboard_dir) if clipboard_dir is not None else None


def _tui_theme_store_for_app(quimera_app) -> TuiThemeStore | None:
    """Resolve o store do tema da TUI persistido no workspace da sessão."""
    workspace = getattr(quimera_app, "workspace", None)
    ui_state_file = getattr(workspace, "ui_state_file", None)
    return TuiThemeStore(ui_state_file) if ui_state_file is not None else None


class QuimeraTextualApp(App):
    """TUI principal do Quimera.

    Recebe o ``QuimeraApp`` (core) e a ``TextualUiBridge`` que liga o loop de
    chat, em thread própria, ao loop de eventos do Textual.
    """

    CSS = TEXTUAL_APP_CSS

    TITLE = "Quimera"

    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+c", "cancel_or_exit", "Cancelar/Sair"),
        ("ctrl+q", "cancel_or_exit", "Sair"),
        ("ctrl+t", "cycle_theme", "Tema"),
        ("alt+t", "cycle_theme", "Tema"),
        ("f6", "cycle_theme", "Tema"),
        ("ctrl+l", "clear_feed", "Limpar chat"),
        ("ctrl+end", "scroll_to_bottom", "Ir ao fim"),
        ("ctrl+home", "scroll_to_top", "Ir ao topo"),
        ("pageup", "feed_page_up", "Rolar feed acima"),
        ("pagedown", "feed_page_down", "Rolar feed abaixo"),
        ("f10", "open_config", "Configurações"),
        ("ctrl+comma", "open_config", "Configurações"),
        ("f9", "open_mcp", "MCP Hub"),
    ]

    def __init__(
        self,
        quimera_app,
        bridge: TextualUiBridge,
        *,
        post_exit_messages: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(driver_class=QuimeraLinuxDriver)
        self._quimera_app = quimera_app
        self._bridge = bridge
        # Erros/avisos vistos no alt-screen, reimpressos em stderr após a saída.
        self._post_exit_messages: list[tuple[str, str]] = (
            post_exit_messages if post_exit_messages is not None else []
        )
        self._worker_thread: threading.Thread | None = None
        self._commands: list[str] = []
        self._summarizing = False
        self._spinner_index = 0
        self._spinner_timer = None
        self._bridge_drain_timer = None
        self._active_agent_timer = None
        self._thinking_pulse_timer = None
        self._thinking_pulse_paused = False
        self._handling_bridge_batch = False
        self._bridge_batch_feed_dirty = False
        self._bridge_batch_feed_force = False
        self._bridge_batch_scroll_end = False
        self._bridge_batch_toolbar_dirty = False
        self._bridge_batch_status_dirty = False
        self._bridge_batch_breadcrumb_dirty = False
        self._bridge_batch_refresh_requested = False
        self._bridge_batch_layout_requested = False
        self.active_agent: str | None = None
        self._last_active_agent_info: tuple[str, str] | None = None
        self._active_tool_previews: dict[str, str] = {}
        self._last_status_bar_state: tuple[tuple[str, str] | None, tuple[tuple[str, str], ...]] | None = None
        self._blank_status_selectors: set[str] = set()
        self._feed_model = TextualFeedModel()
        self._history_file_path: Path | None = None
        self._restored_history_hydrated = False
        self._breadcrumb_chain: list[str] = []
        self._feed_renderable_cache: dict[int, object] = {}
        self._scroll_feed_on_submit = False
        self._tui_theme_store = _tui_theme_store_for_app(self._quimera_app)
        self._tui_theme_persisted: str | None = None

    def compose(self) -> ComposeResult:
        yield _SummaryHeader(show_clock=True, id="header")
        with Vertical(id="main"):
            yield _UnifiedFeed(id="feed")
            yield Static("", id="toolbar")
            yield Static("", id="status_bar")
            yield Static("", id="question_overlay")
            yield CompletionDropdown()
        yield Static("", id="agent_status")
        with Horizontal(id="input_bar"):
            yield _CompletionInput(
                id="input",
                clipboard_paste_handler=lambda: _read_clipboard_for_input(
                    _clipboard_dir_for_app(self._quimera_app)
                ),
            )

    def on_mount(self) -> None:
        self._restore_tui_theme()
        self._bridge.attach_textual_app(self)
        self._bridge.set_input_value("")
        gate = getattr(self._quimera_app, "input_gate", None)
        if hasattr(gate, "set_textual_mounted"):
            gate.set_textual_mounted(True)
        self._drain_bridge_events(max_events=None)
        self._bridge_drain_timer = self.set_interval(0.05, self._drain_bridge_events)
        self._active_agent_timer = self.set_interval(0.2, self._poll_active_agent)
        self._thinking_pulse_timer = self.set_interval(0.25, self._pulse_thinking_marker)
        renderer = getattr(self._quimera_app, "renderer", None)
        if renderer is not None:
            _orq = getattr(getattr(self._quimera_app, "agent_pool", None), "orchestrator_agent", None)
            if _orq:
                renderer.set_orchestrator(_orq)
        self._worker_thread = threading.Thread(
            target=self._run_quimera_app,
            daemon=True,
            name="quimera-textual-loop",
        )
        self._worker_thread.start()
        input_widget = self.query_one("#input", _CompletionInput)
        input_widget.set_prefix(
            PromptFormatter.format_user_prompt(getattr(self._quimera_app, "user_name", None))
        )
        history_file = getattr(gate, "_history_file", None)
        self._history_file_path = Path(history_file).expanduser() if history_file else None
        input_widget.load_history(self._history_file_path)
        input_widget.focus()

    def _restore_tui_theme(self) -> None:
        """Aplica o tema do Textual persistido para este workspace, se houver."""
        store = self._tui_theme_store
        stored = store.theme if store is not None else None
        if stored and stored in self.available_themes:
            self._tui_theme_persisted = stored
            self.theme = stored

    def watch_theme(self, theme_name: str) -> None:
        """Persiste por workspace o tema escolhido na palette do Textual."""
        store = self._tui_theme_store
        if store is None or theme_name == self._tui_theme_persisted:
            return
        self._tui_theme_persisted = theme_name
        try:
            store.set_theme(theme_name)
        except OSError:
            _logger.warning("Falha ao persistir o tema da TUI", exc_info=True)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Comandos da command palette do Textual."""
        yield from super().get_system_commands(screen)
        yield SystemCommand("Configurações", "Abrir tela de configurações", self.action_open_config)
        yield SystemCommand("MCP Hub", "Gerenciar conexões e transportes MCP", self.action_open_mcp)

    def action_open_config(self) -> None:
        """Abre a janela popup de configurações."""
        self.push_screen(ConfigScreen(self._quimera_app, self))

    def action_open_mcp(self) -> None:
        """Abre o hub de conexões MCP."""
        self.push_screen(MCPConnectionsScreen(self._quimera_app, self))

    def action_open_external_url(self, url: str) -> None:
        """Abre um link do feed (ex.: autorização OAuth) no navegador.

        Sem navegador gráfico alcançável, tenta copiar o link para a área
        de transferência via OSC 52 e avisa o usuário.
        """
        target = str(url or "").strip()
        if not target:
            return
        if open_in_browser(target):
            self.notify("Link aberto no navegador.", timeout=4, markup=False)
            return
        try:
            self.copy_to_clipboard(target)
        except Exception:
            _logger.debug("copy_to_clipboard indisponível", exc_info=True)
        self.notify(
            "Navegador indisponível; o link foi copiado para a área de "
            "transferência (se o terminal suportar OSC 52).",
            severity="warning",
            timeout=8,
            markup=False,
        )

    def on_unmount(self) -> None:
        gate = getattr(self._quimera_app, "input_gate", None)
        if hasattr(gate, "set_textual_mounted"):
            gate.set_textual_mounted(False)
        if self._bridge_drain_timer is not None:
            try:
                self._bridge_drain_timer.stop()
            except Exception as exc:
                _logger.debug("on_unmount: parar o timer da bridge falhou: %s", exc)
            self._bridge_drain_timer = None
        if self._active_agent_timer is not None:
            try:
                self._active_agent_timer.stop()
            except Exception as exc:
                _logger.debug("on_unmount: parar o timer de agente ativo falhou: %s", exc)
            self._active_agent_timer = None
        if self._thinking_pulse_timer is not None:
            try:
                self._thinking_pulse_timer.stop()
            except Exception as exc:
                _logger.debug("on_unmount: parar o timer de pulso falhou: %s", exc)
            self._thinking_pulse_timer = None
        try:
            self.query_one("#input", _CompletionInput).save_history(self._history_file_path)
        except NoMatches:
            _logger.debug("on_unmount: input já desmontado; histórico não salvo")
        except Exception as exc:
            _logger.warning("on_unmount: salvar o histórico do input falhou: %s", exc)
        self._bridge.detach_textual_app(self)

    def _hydrate_restored_history(self) -> bool:
        """Carrega o histórico já restaurado pelo core no feed visual."""
        if self._restored_history_hydrated:
            return False
        history = getattr(self._quimera_app, "history", None)
        if not history:
            return False
        self._restored_history_hydrated = True
        resolver = getattr(self._quimera_app, "_resolve_profile_style", None)
        renderer = getattr(self._quimera_app, "renderer", None)
        return self._feed_model.hydrate_from_history(
            list(history),
            user_label=str(getattr(self._quimera_app, "user_name", ">>>") or ">>>"),
            agent_resolver=resolver if callable(resolver) else None,
            theme=getattr(renderer, "theme_name", None),
        )

    def _status_preview_text(self, value) -> str:
        if isinstance(value, dict):
            for key in ("content", "message", "preview"):
                text = str(value.get(key) or "").strip()
                if text:
                    value = text
                    break
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

    def _blank_status_widget(self, selector: str) -> bool:
        """Esconde e esvazia um Static de status só na primeira vez.

        ``#status_bar`` e ``#agent_status`` nunca voltam a ser exibidos;
        repetir ``update("")`` a cada poll de 0,2 s pedia layout da tela
        inteira em idle sem mudar nada visível.
        """
        if selector in self._blank_status_selectors:
            return True
        try:
            widget = self.query_one(selector, Static)
        except NoMatches:
            return False
        widget.display = False
        widget.update("")
        self._blank_status_selectors.add(selector)
        return True

    def _update_agent_status_widget(self) -> bool:
        if self._handling_bridge_batch:
            self._bridge_batch_status_dirty = True
            return True
        return self._blank_status_widget("#agent_status")

    def _update_breadcrumb(self) -> None:
        if self._handling_bridge_batch:
            self._bridge_batch_breadcrumb_dirty = True
            return
        chain = self._breadcrumb_chain
        if chain:
            breadcrumb_text = " > ".join(chain[:5])
            try:
                self.query_one("#breadcrumb", _BreadcrumbWidget).update(f"  {breadcrumb_text}")
            except NoMatches:
                return
        else:
            try:
                self.query_one("#breadcrumb", _BreadcrumbWidget).update("")
            except NoMatches:
                return

    def _clear_status_bar(self) -> None:
        self._active_tool_previews.clear()
        self._last_active_agent_info = None
        self._last_status_bar_state = None
        self._breadcrumb_chain = []
        self.active_agent = None
        self._update_breadcrumb()
        if self._handling_bridge_batch:
            self._bridge_batch_status_dirty = True
            return
        if not self._blank_status_widget("#status_bar"):
            return
        self._update_agent_status_widget()

    def _update_status_bar(self) -> None:
        if self._handling_bridge_batch:
            self._bridge_batch_status_dirty = True
            return
        if not self._update_agent_status_widget():
            return
        info = self._bridge.active_agent_info()
        tools = tuple(self._active_tool_previews.items())
        state = (info, tools)
        if state == self._last_status_bar_state:
            return
        self._last_status_bar_state = state
        self._last_active_agent_info = info
        self._blank_status_widget("#status_bar")

    def _poll_active_agent(self) -> None:
        self._update_status_bar()

    def _drain_bridge_events(self, *, max_events: int | None = 256) -> None:
        if self._handling_bridge_batch:
            return
        events = self._bridge.drain_pending_events(max_items=max_events)
        if not events:
            return

        # Uma rajada de stream/tool events deve compor um único frame. Os
        # handlers ainda aplicam tudo em ordem, mas feed, toolbar e repaint
        # são reconciliados só após o lote completo.
        self._handling_bridge_batch = True
        try:
            for event in events:
                try:
                    self.handle_bridge_event(event)
                except Exception:
                    # Um evento inválido não deve descartar o restante da
                    # rajada que já saiu da fila.
                    _logger.exception(
                        "Falha ao processar evento Textual %s",
                        getattr(event, "kind", "unknown"),
                    )
        finally:
            self._handling_bridge_batch = False

        feed_dirty = self._bridge_batch_feed_dirty
        feed_force = self._bridge_batch_feed_force
        scroll_end = self._bridge_batch_scroll_end
        toolbar_dirty = self._bridge_batch_toolbar_dirty
        status_dirty = self._bridge_batch_status_dirty
        breadcrumb_dirty = self._bridge_batch_breadcrumb_dirty
        refresh_requested = self._bridge_batch_refresh_requested
        layout_requested = self._bridge_batch_layout_requested
        self._bridge_batch_feed_dirty = False
        self._bridge_batch_feed_force = False
        self._bridge_batch_scroll_end = False
        self._bridge_batch_toolbar_dirty = False
        self._bridge_batch_status_dirty = False
        self._bridge_batch_breadcrumb_dirty = False
        self._bridge_batch_refresh_requested = False
        self._bridge_batch_layout_requested = False

        if feed_dirty:
            try:
                self._sync_feed(force=feed_force, scroll_end=scroll_end)
            except Exception:
                _logger.exception("Falha ao reconciliar feed Textual")
        if toolbar_dirty:
            try:
                self._refresh_toolbar()
            except Exception:
                _logger.exception("Falha ao reconciliar toolbar Textual")
        if breadcrumb_dirty:
            try:
                self._update_breadcrumb()
            except Exception:
                _logger.exception("Falha ao reconciliar breadcrumb Textual")
        if status_dirty:
            try:
                self._update_status_bar()
            except Exception:
                _logger.exception("Falha ao reconciliar status Textual")
        if feed_dirty or toolbar_dirty or status_dirty or breadcrumb_dirty or refresh_requested:
            self._refresh_now(layout=layout_requested)

    def flush_bridge_events(self) -> None:
        # Drena até o watermark observado agora. Eventos produzidos durante
        # o flush ficam para o próximo frame em vez de prolongá-lo sem fim.
        self._drain_bridge_events(max_events=self._bridge.pending_event_count())

    def _start_spinner(self) -> None:
        """Inicia feedback visual de loading para sumarização."""
        if self._spinner_timer is not None:
            return
        self._summarizing = True
        self._spinner_index = 0
        self.notify(
            f"{_SUMMARY_SPINNER_FRAMES[0]} {_SUMMARY_NOTIFICATION_MESSAGE}",
            severity="information",
            timeout=4,
            markup=False,
        )
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

    def _pause_thinking_pulse(self) -> None:
        """Suspende o timer do pulso quando não há nada para animar.

        O timer fica bloqueado no ``Event`` interno do Textual, sem agendar
        wakeups; o ritmo de 0,25 s não muda enquanto houver atividade.
        """
        timer = self._thinking_pulse_timer
        if timer is None or self._thinking_pulse_paused:
            return
        try:
            timer.pause()
        except Exception:
            _logger.debug("Falha ao pausar o pulso de pensamento", exc_info=True)
            return
        self._thinking_pulse_paused = True

    def _resume_thinking_pulse(self) -> None:
        """Retoma o pulso ao reconciliar o feed; o primeiro tick vem em 0,25 s."""
        if not self._thinking_pulse_paused:
            return
        timer = self._thinking_pulse_timer
        if timer is None:
            self._thinking_pulse_paused = False
            return
        try:
            # reset() reativa o timer e recomeça a grade a partir de agora,
            # em vez de disparar um tick atrasado imediatamente.
            timer.reset()
        except Exception:
            _logger.debug("Falha ao retomar o pulso de pensamento", exc_info=True)
            return
        self._thinking_pulse_paused = False

    def _pulse_thinking_marker(self) -> None:
        """Anima o marcador de pensamento e a esfera do turno humano em execução."""
        expired_tools = self._feed_model.expire_tool_previews()
        if expired_tools:
            self._sync_feed()
            self._refresh_now()
        active_submissions = self._feed_model.active_submission_items()
        if not self._feed_model.has_transients and not active_submissions:
            reset_thinking_pulse()
            if not expired_tools:
                # Sem transients, submissões nem expiry neste tick não há o
                # que animar: dorme até o próximo _sync_feed retomar.
                self._pause_thinking_pulse()
            return
        advance_thinking_pulse()
        self._sync_transient_feed_slots()
        if active_submissions:
            self._sync_submission_feed_slots(active_submissions)

    def _run_quimera_app(self) -> None:
        try:
            self._quimera_app.run()
        except Exception as exc:  # noqa: BLE001
            self._post_exit_messages.append(
                ("error", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            )
        finally:
            try:
                self.call_from_thread(self.exit)
            except RuntimeError:
                return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # Só o input fixo do chat pode despachar mensagens; submissões de
        # inputs de telas auxiliares (ex: configuração) nunca viram chat.
        if not isinstance(event.input, _CompletionInput) or event.input.id != "input":
            return
        try:
            self.query_one(CompletionDropdown).hide()
        except Exception as exc:
            _logger.debug("on_input_submitted: esconder o dropdown falhou: %s", exc)
        value = event.input.submission_value
        event.input.reset_to_prefix()
        self._bridge.set_input_value("")
        if value:
            event.input.add_to_history(value)
            event.input.save_history(self._history_file_path)
        self._bridge.submit_input(value)
        if value:
            # Enviar um prompt reengata a âncora do feed: o scroll vai ao
            # fim agora e o compositor segue o fim até o usuário rolar.
            self._scroll_feed_on_submit = True
            self.query_one("#feed", _UnifiedFeed).anchor()

    def _set_question_overlay(self, payload) -> None:
        overlay = self.query_one("#question_overlay", Static)
        overlay.update(_build_question_overlay(payload))
        overlay.display = True
        if len(self.screen_stack) == 1:
            _restore_textual_input_focus(self)

    def _clear_question_overlay(self) -> None:
        overlay = self.query_one("#question_overlay", Static)
        _clear_question_overlay_widget(overlay)

    def _clear_prompt_state(self) -> None:
        gate = getattr(self._quimera_app, "input_gate", None)
        clearer = getattr(gate, "clear_interactive_prompt_state", None)
        if callable(clearer):
            clearer()
        self._refresh_toolbar()

    def _refresh_now(self, *, layout: bool = False) -> None:
        if self._handling_bridge_batch:
            self._bridge_batch_refresh_requested = True
            self._bridge_batch_layout_requested |= layout
            return
        try:
            self.refresh(layout=layout)
        except Exception:
            _logger.exception("Falha ao atualizar a interface Textual")
            return

    def _refresh_toolbar(self) -> None:
        if self._handling_bridge_batch:
            self._bridge_batch_toolbar_dirty = True
            return
        gate = getattr(self._quimera_app, "input_gate", None)
        builder = getattr(gate, "_build_toolbar_renderable", None)
        if callable(builder):
            toolbar = self.query_one("#toolbar", Static)
            max_width = max(0, int(getattr(toolbar.size, "width", 0) or 0) - 2)
            try:
                toolbar.update(builder(max_width=max_width))
            except TypeError:
                toolbar.update(builder())

    def action_cancel_or_exit(self) -> None:
        self._bridge.cancel_or_exit()

    def action_cycle_theme(self) -> None:
        gate = getattr(self._quimera_app, "input_gate", None)
        handler = getattr(gate, "_theme_cycle_handler", None)
        if callable(handler):
            handler()
        self._refresh_toolbar()

    def action_clear_feed(self) -> None:
        self._clear_chat_feed()

    def _clear_chat_feed(self) -> None:
        """Limpa o chat e mantém o cabeçalho de boot no topo do feed.

        Os slots do cabeçalho (logo, projeto, bloco MCP, caminhos de log)
        continuam montados; a reconciliação por posição remove apenas os
        slots excedentes, que eram o chat.
        """
        self._feed_model.clear_chat()
        self._clear_status_bar()
        self._sync_feed(scroll_end=True)
        self._refresh_now(layout=True)
        self.audit_event(
            "tui_clear_chat",
            kept_items=len(self._feed_model.items),
            **feed_geometry(self),
        )

    def audit_event(self, event: str, **payload) -> None:
        """Grava um evento da TUI no audit de render (só com ``--debug``).

        O renderer é o dono do logger; sem ele (sem debug, ou app de
        teste sem renderer) a chamada é no-op. Widgets usam este ponto
        para registrar correções silenciosas, como o clamp de scroll.
        """
        renderer = getattr(self._quimera_app, "renderer", None)
        log_event = getattr(renderer, "log_debug_event", None)
        if not callable(log_event):
            return
        try:
            log_event(event, **payload)
        except Exception:
            _logger.debug("Falha ao gravar evento de audit da TUI", exc_info=True)

    def action_scroll_to_bottom(self) -> None:
        feed = self.query_one("#feed", _UnifiedFeed)
        feed.scroll_end(animate=False)

    def action_scroll_to_top(self) -> None:
        feed = self.query_one("#feed", _UnifiedFeed)
        feed.scroll_home(animate=False)

    def action_feed_page_up(self) -> None:
        feed = self.query_one("#feed", _UnifiedFeed)
        feed.scroll_page_up(animate=False)

    def action_feed_page_down(self) -> None:
        feed = self.query_one("#feed", _UnifiedFeed)
        feed.scroll_page_down(animate=False)

    def _sync_feed(self, *, force: bool = False, scroll_end: bool = False) -> None:
        """Reconcilia histórico e execuções vivas na mesma área rolável."""
        if self._handling_bridge_batch:
            self._bridge_batch_feed_dirty = True
            self._bridge_batch_feed_force |= force
            self._bridge_batch_scroll_end |= scroll_end
            return
        # Qualquer reconciliação real pode ter criado transients, submissões
        # ou expiries; o pulso decide no próximo tick se volta a dormir.
        self._resume_thinking_pulse()
        feed = self.query_one("#feed", _UnifiedFeed)
        entries: list[tuple[int, bool, object, bool]] = []
        active_tokens: set[int] = set()
        for item in self._feed_model.items:
            token = id(item.event)
            active_tokens.add(token)
            renderable = self._feed_renderable_cache.get(token)
            if force or item.transient or renderable is None:
                renderable = _render_event(item.event)
                if renderable is not None:
                    self._feed_renderable_cache[token] = renderable
            if renderable is not None:
                entries.append(
                    (
                        token,
                        item.transient,
                        renderable,
                        bool(item.event.compact),
                    )
                )

        self._feed_renderable_cache = {
            token: renderable
            for token, renderable in self._feed_renderable_cache.items()
            if token in active_tokens
        }
        if self._scroll_feed_on_submit:
            # O envio de prompt força o scroll ao fim exatamente uma vez,
            # mesmo que o usuário estivesse lendo o histórico lá em cima.
            self._scroll_feed_on_submit = False
            scroll_end = True
        feed.sync_entries(entries, force=force)
        if scroll_end:
            # Reengata a âncora; o acompanhamento contínuo do fim fica a
            # cargo do compositor enquanto o feed estiver ancorado.
            feed.anchor()

    def _sync_transient_feed_slots(self) -> None:
        """Atualiza somente slots transitórios quando apenas o pulso mudou."""
        feed = self.query_one("#feed", _UnifiedFeed)
        for index, item in self._feed_model.transient_items():
            token = id(item.event)
            renderable = _render_event(item.event)
            if renderable is None or not feed.update_entry(index, token, renderable):
                self._sync_feed()
                return
            self._feed_renderable_cache[token] = renderable

    def _sync_submission_feed_slots(self, slots) -> None:
        """Re-renderiza turnos humanos com submissão ativa para a esfera pulsar."""
        feed = self.query_one("#feed", _UnifiedFeed)
        for index, item in slots:
            token = id(item.event)
            renderable = _render_event(item.event)
            if renderable is None or not feed.update_entry(index, token, renderable):
                self._sync_feed()
                return
            self._feed_renderable_cache[token] = renderable

    def _redraw_feed(self, *, scroll_end: bool = False) -> None:
        """Reconstrói os slots visuais, usado na restauração de histórico."""
        self._feed_renderable_cache.clear()
        self._sync_feed(force=True, scroll_end=scroll_end)
        self._refresh_now(layout=True)

    def on_input_changed(self, event: Input.Changed) -> None:
        if not isinstance(event.input, _CompletionInput):
            return
        dropdown = self.query_one(CompletionDropdown)
        value = event.input.user_value if isinstance(event.input, _CompletionInput) else str(event.value)
        self._bridge.set_input_value(value)

        if not value:
            dropdown.hide()
            return
        if " " in value and not value.startswith(("/", "s/", "r/", "o/")):
            dropdown.hide()
            return
        if value.startswith(("/", "s/", "r/", "o/")):
            gate = getattr(self._quimera_app, "input_gate", None)
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
        _append_post_exit_failure_message(self._post_exit_messages, event)
        self._record_status_event(event)
        self._update_status_bar()
        if event.kind == "clear":
            self._clear_chat_feed()
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
        if event.kind == "open_config":
            self.action_open_config()
            return
        if event.kind == "open_connection_config":
            payload = event.payload if isinstance(event.payload, dict) else {}
            agent_name = str(payload.get("agent") or event.agent or "").strip()
            if agent_name:
                self.push_screen(
                    ConnectionScreen(
                        self._quimera_app,
                        self,
                        agent_name,
                        advanced=bool(payload.get("advanced", False)),
                    )
                )
            return
        if event.kind == "prompt_preview":
            payload = event.payload or {}
            self.push_screen(
                PromptPreviewScreen(
                    str(payload.get("agent") or event.agent or "agente"),
                    str(payload.get("preview") or ""),
                )
            )
            return
        if event.kind == "text_window":
            payload = event.payload if isinstance(event.payload, dict) else {}
            self.push_screen(
                PromptPreviewScreen(
                    str(payload.get("agent") or event.agent or ""),
                    str(payload.get("content") or ""),
                    title=str(payload.get("title") or "") or None,
                )
            )
            return
        if event.kind == "notification":
            payload = event.payload or {}
            message = str(payload.get("message") or "").strip()
            if message:
                self.notify(
                    message,
                    severity=str(payload.get("severity") or "information"),
                    timeout=payload.get("timeout"),
                    markup=False,
                )
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
            if callable(getattr(getattr(self._quimera_app, "input_gate", None), "_build_toolbar_renderable", None)):
                self._refresh_toolbar()
            else:
                self.query_one("#toolbar", Static).update(toolbar)
            if len(self.screen_stack) == 1:
                # Não rouba o foco de telas modais (ex: configuração).
                self.query_one("#input", Input).focus()
            self._clear_status_bar()
            self._refresh_now(layout=True)
            return
        if not self._feed_model.apply(event):
            return
        self._sync_feed()
        self._refresh_toolbar()
        self._refresh_now()


def run_textual_quimera_app(quimera_app, bridge: TextualUiBridge) -> None:
    """Executa a interface Textual como UI principal do Quimera."""
    post_exit_messages: list[tuple[str, str]] = []
    bridge.attach_quimera_app(quimera_app)
    renderer = getattr(quimera_app, "renderer", None)
    if renderer is not None:
        renderer.set_profile_resolver(quimera_app._resolve_profile_style)
    try:
        QuimeraTextualApp(quimera_app, bridge, post_exit_messages=post_exit_messages).run(
            mouse=not _is_android()
        )
    finally:
        bridge.submission_tracker.close()
        _restore_terminal_modes()

    # Após o Textual sair (tela alternativa restaurada), drena eventos pendentes
    # e imprime erros/warnings para a tela normal — assim não desaparecem.
    _screen_handler.drain_to_stderr()
    for _ev in bridge.drain_pending_events():
        _append_post_exit_failure_message(post_exit_messages, _ev)
    for _kind, _content in post_exit_messages:
        if _content:
            print(_content, file=sys.stderr, flush=True)
