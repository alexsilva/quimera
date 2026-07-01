"""Interface Textual principal do Quimera."""
from __future__ import annotations

import logging
import queue
import shutil
import sys
import traceback
import threading
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import quimera.themes as themes
from quimera.ui.text import (
    _apply_stream_diff,
    _extract_text_from_renderable,
    _normalize_stream_diff,
    strip_ansi,
)
from quimera.app.config import handler as _screen_handler
from quimera.constants import CMD_EXIT

_NO_RESPONSE_MESSAGE = "sem resposta válida"
_SUMMARY_SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
_APPROVAL_TITLE = "Permissão solicitada"
_APPROVAL_OPTIONS = (
    "s/sim/y/yes = aprovar",
    "n/não/no/enter = negar",
    "a/all/todas = aprovar todas",
)
_TERMINAL_MODE_RESET = (
    "\x1b[?1000l"  # mouse click tracking
    "\x1b[?1002l"  # mouse button-event tracking
    "\x1b[?1003l"  # any-event mouse tracking
    "\x1b[?1005l"  # UTF-8 mouse mode
    "\x1b[?1006l"  # SGR mouse mode
    "\x1b[?1015l"  # urxvt mouse mode
    "\x1b[?2004l"  # bracketed paste
    "\x1b[?25h"    # cursor visible
)


def _restore_terminal_modes() -> None:
    """Desativa modos interativos que não podem vazar para editor/shell."""
    stdout = getattr(sys, "__stdout__", None) or sys.stdout
    if stdout is None:
        return
    try:
        stdout.write(_TERMINAL_MODE_RESET)
        stdout.flush()
    except Exception:
        return


def _restore_textual_input_focus(textual_app) -> None:
    """Restaura foco e cursor do input fixo depois de janelas externas."""
    if textual_app is None:
        return
    try:
        input_widget = textual_app.query_one("#input")
    except Exception:
        return
    try:
        input_widget.focus()
    except Exception:
        pass
    try:
        input_widget.cursor_position = len(str(getattr(input_widget, "value", "") or ""))
    except Exception:
        pass


def _approval_options() -> list[str]:
    """Retorna as opções visuais padrão para confirmação de permissão."""
    return list(_APPROVAL_OPTIONS)


def _build_question_overlay(payload) -> Panel:
    """Monta o overlay visual de pergunta usado pela UI Textual."""
    data = payload or {}
    question = str(data.get("question", "")).strip()
    kind = str(data.get("kind", "input")).strip().lower()
    title = str(data.get("title") or "").strip()
    options = list(data.get("options", []) or [])
    if kind == "approval":
        title = title or _APPROVAL_TITLE
        options = options or _approval_options()
    elif not title:
        title = "input solicitado"

    lines = [question] if question else []
    if options:
        if lines:
            lines.append("")
        lines.append("Opções:")
        lines.extend(f"- {option}" for option in options)

    body = "\n".join(lines) if lines else "Aguardando resposta..."
    border_style = "bold yellow" if kind == "approval" else "yellow"
    return Panel(body, title=title, border_style=border_style)


def _build_window_overlay_payload(payload) -> dict[str, Any]:
    """Converte evento de janela interativa no payload do overlay."""
    data = dict(payload or {}) if isinstance(payload, dict) else {}
    metadata = dict(data.get("metadata") or {}) if isinstance(data.get("metadata"), dict) else {}
    kind = str(data.get("kind") or "input")
    title = str(data.get("title") or (_APPROVAL_TITLE if kind == "approval" else "input solicitado"))
    question = str(metadata.get("question") or data.get("question") or "")
    options = data.get("options")
    if kind == "approval" and not options:
        options = _approval_options()
    return {
        "question": question,
        "options": list(options or []),
        "title": title,
        "kind": kind,
        "owner": data.get("owner"),
    }


def _clear_question_overlay_widget(overlay) -> None:
    """Remove o overlay de pergunta/permissão do widget Textual."""
    overlay.update("")
    overlay.display = False


@contextmanager
def _external_textual_window(textual_app):
    """Suspende Textual para processo externo sem vazar modos de terminal."""
    if textual_app is None:
        _restore_terminal_modes()
        try:
            yield
        finally:
            _restore_terminal_modes()
        return

    driver = getattr(textual_app, "_driver", None)
    call_from_thread = getattr(textual_app, "call_from_thread", None)

    if driver is None or not callable(call_from_thread):
        suspend = getattr(textual_app, "suspend", None)
        if callable(suspend):
            with suspend():
                _restore_terminal_modes()
                try:
                    yield
                finally:
                    _restore_terminal_modes()
            _restore_textual_input_focus(textual_app)
            return
        _restore_terminal_modes()
        try:
            yield
        finally:
            _restore_terminal_modes()
        return

    can_suspend = bool(getattr(driver, "can_suspend", False))

    def _suspend_driver() -> None:
        if not can_suspend:
            return
        try:
            textual_app._suspend_signal()
        except Exception:
            pass
        driver.suspend_application_mode()

    def _resume_driver() -> None:
        if not can_suspend:
            return
        driver.resume_application_mode()
        try:
            textual_app._resume_signal()
        except Exception:
            pass
        try:
            textual_app.refresh(layout=True)
        except Exception:
            pass
        _restore_textual_input_focus(textual_app)

    try:
        call_from_thread(_suspend_driver)
    except Exception:
        _restore_terminal_modes()
        try:
            yield
        finally:
            _restore_terminal_modes()
        return

    _restore_terminal_modes()
    try:
        yield
    finally:
        _restore_terminal_modes()
        resumed = False
        try:
            call_from_thread(_resume_driver)
            resumed = True
        finally:
            if not resumed:
                _restore_terminal_modes()


class _TextualConsoleShim:
    """Console mínimo para código legado que ainda chama ``console.print``."""

    def __init__(self, bridge: "TextualUiBridge") -> None:
        self._bridge = bridge

    def print(self, *objects, sep: str = " ", end: str = "\n", **kwargs) -> None:
        """Roteia prints Rich/legados para o feed Textual."""
        message = sep.join(str(obj) for obj in objects)
        if end and end != "\n":
            message = f"{message}{end}"
        self._bridge.emit(TextualUiEvent("plain", message))


class _TextualStatus:
    """Context manager simples para contratos running_status/live_status no Textual."""

    def __init__(self, renderer: "TextualRenderer", agent: str | None = None, initial: str = "") -> None:
        self._renderer = renderer
        self._agent = agent
        self._initial = initial

    def update(self, text: str) -> None:
        self._renderer.update_status(self._agent, text)

    def __enter__(self):
        if self._initial:
            self.update(self._initial)
        return self

    def __exit__(self, *args) -> None:
        self._renderer.update_status(self._agent, "concluído")


@dataclass
class TextualUiEvent:
    """Evento thread-safe enviado do runtime para a UI Textual."""

    kind: str
    payload: Any = None
    agent: str | None = None


@dataclass
class TextualFeedItem:
    """Item lógico do feed Textual."""

    event: TextualUiEvent
    transient: bool = False


@dataclass(frozen=True)
class TextualFeedChange:
    """Resultado da aplicação de um evento no feed Textual."""

    changed: bool
    redraw: bool = False
    appended: TextualFeedItem | None = None


class TextualFeedModel:
    """Modelo testável do feed: transitórios por agente são substituíveis."""

    _TRANSIENT_KINDS = {"stream_start", "stream_chunk", "stream_abort", "agent_update", "agent_lifecycle", "pending_input"}

    _IGNORED_KINDS = {
        "prompt",
        "prompt_clear",
        "input_active",
        "summarizing",
        "window_open",
        "window_clear",
        "theme_changed",
    }

    def __init__(self) -> None:
        self._items: list[TextualFeedItem] = []
        self._transient_index_by_agent: dict[str, int] = {}
        self._stream_buffer_by_agent: dict[str, str] = {}
        self._stream_meta_by_agent: dict[str, dict[str, Any]] = {}
        self._finalized_agents: set[str] = set()
        self._last_change = TextualFeedChange(False)

    @property
    def items(self) -> list[TextualFeedItem]:
        """Snapshot dos itens atuais do feed."""
        return list(self._items)

    @property
    def last_change(self) -> TextualFeedChange:
        """Última mudança aplicada ao feed."""
        return self._last_change

    def clear(self) -> None:
        """Limpa estado do feed."""
        self._items.clear()
        self._transient_index_by_agent.clear()
        self._stream_buffer_by_agent.clear()
        self._stream_meta_by_agent.clear()
        self._finalized_agents.clear()
        self._last_change = TextualFeedChange(True, redraw=True)

    def apply(self, event: TextualUiEvent) -> bool:
        """Aplica evento e retorna se o feed visual precisa ser redesenhado."""
        self._last_change = TextualFeedChange(False)
        if event.kind in self._IGNORED_KINDS:
            return False
        if event.kind in {"question", "question_clear"}:
            return False
        if event.kind == "visual_reset":
            return self._apply_visual_reset(event)
        if event.kind == "agent_message":
            replaced = self._replace_transient_with_final(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        if event.kind == "stream_start":
            agent = self._agent_key(event)
            self._finalized_agents.discard(agent)
            self._stream_buffer_by_agent[agent] = ""
            self._stream_meta_by_agent[agent] = dict(event.payload or {}) if isinstance(event.payload, dict) else {}
            replaced = self._upsert_transient(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        if event.kind == "stream_chunk":
            return self._apply_stream_chunk(event)
        if event.kind in self._TRANSIENT_KINDS:
            if self._is_late_completed_lifecycle(event):
                return False
            replaced = self._upsert_transient(event)
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        item = TextualFeedItem(event, transient=False)
        self._items.append(item)
        self._last_change = TextualFeedChange(True, appended=item)
        return True

    def _agent_key(self, event: TextualUiEvent) -> str:
        return str(event.agent or "__global__")

    def _upsert_transient(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        item = TextualFeedItem(event, transient=True)
        index = self._transient_index_by_agent.get(agent)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True
        self._transient_index_by_agent[agent] = len(self._items)
        self._items.append(item)
        return False

    def _replace_transient_with_final(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        self._stream_buffer_by_agent.pop(agent, None)
        self._stream_meta_by_agent.pop(agent, None)
        self._finalized_agents.add(agent)
        item = TextualFeedItem(event, transient=False)
        index = self._transient_index_by_agent.pop(agent, None)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return True
        self._items.append(item)
        return False

    def _is_late_completed_lifecycle(self, event: TextualUiEvent) -> bool:
        if event.kind != "agent_lifecycle":
            return False
        agent = self._agent_key(event)
        if agent not in self._finalized_agents:
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("status", "")).strip().lower() == "completed"

    def _apply_stream_chunk(self, event: TextualUiEvent) -> bool:
        agent = self._agent_key(event)
        current = self._stream_buffer_by_agent.get(agent, "")
        payload = event.payload
        if isinstance(payload, dict):
            diff = _normalize_stream_diff(payload.get("diff"))
            if diff:
                current = _apply_stream_diff(current, diff)
            elif payload.get("text"):
                current += strip_ansi(str(payload.get("text")))
            else:
                current += strip_ansi(str(payload))
        else:
            current += strip_ansi(str(payload))
        self._stream_buffer_by_agent[agent] = current
        if current.strip():
            payload: Any = current
            meta = self._stream_meta_by_agent.get(agent)
            if meta:
                payload = {**meta, "content": current}
            replaced = self._upsert_transient(TextualUiEvent("stream_chunk", payload, agent=event.agent))
            self._last_change = TextualFeedChange(True, redraw=replaced, appended=None if replaced else self._items[-1])
            return True
        self._last_change = TextualFeedChange(False)
        return False

    def _apply_visual_reset(self, event: TextualUiEvent) -> bool:
        """Remove estado visual transitório sem apagar mensagens persistentes."""
        agent = str(event.agent or "").strip()
        if agent:
            index = self._transient_index_by_agent.pop(agent, None)
            self._stream_buffer_by_agent.pop(agent, None)
            self._stream_meta_by_agent.pop(agent, None)
            if index is None or not (0 <= index < len(self._items)):
                self._last_change = TextualFeedChange(False)
                return False
            del self._items[index]
            self._reindex_transients()
            self._last_change = TextualFeedChange(True, redraw=True)
            return True

        before = len(self._items)
        self._items = [item for item in self._items if not item.transient]
        self._transient_index_by_agent.clear()
        self._stream_buffer_by_agent.clear()
        self._stream_meta_by_agent.clear()
        changed = len(self._items) != before
        self._last_change = TextualFeedChange(changed, redraw=changed)
        return changed

    def _reindex_transients(self) -> None:
        self._transient_index_by_agent.clear()
        for index, item in enumerate(self._items):
            if item.transient:
                self._transient_index_by_agent[self._agent_key(item.event)] = index


def _resolve_textual_feed_limit(quimera_app) -> int | None:
    """Retorna o limite visual do feed Textual.

    O feed é scrollback visual, não janela de contexto. Configurações como
    history_window e auto_summarize_threshold limitam memória/prompt, mas não
    podem truncar a saída rolável dos agentes.
    """
    return None


def _append_post_exit_failure_message(
    messages: list[tuple[str, str]],
    event: "TextualUiEvent",
) -> bool:
    """Guarda falhas exibidas no alt-screen para reimpressão após a saída."""
    if event.kind not in {"error", "warning"}:
        return False
    content = str(event.payload or "").strip()
    if not content:
        return False
    messages.append((event.kind, content))
    return True


class TextualUiBridge:
    """Bridge thread-safe entre o loop legado do Quimera e o app Textual."""

    def __init__(self) -> None:
        self.input_queue: queue.Queue[str] = queue.Queue()
        self.direct_input_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[TextualUiEvent] = queue.Queue()
        self.textual_app = None
        self.quimera_app = None
        self._input_value = ""
        self._active_agent_labels: dict[str, str] = {}
        self._direct_input_depth = 0
        self._lock = threading.Lock()

    def attach_textual_app(self, textual_app) -> None:
        """Registra a instância Textual ativa."""
        with self._lock:
            self.textual_app = textual_app

    def attach_quimera_app(self, quimera_app) -> None:
        """Registra a instância Quimera controlada pela UI."""
        with self._lock:
            self.quimera_app = quimera_app

    def create_renderer(self) -> "TextualRenderer":
        """Cria renderer compatível com o contrato usado pelo Quimera."""
        return TextualRenderer(self)

    def create_input_gate(self, **kwargs) -> "TextualInputGate":
        """Cria input gate compatível com o contrato usado pelo Quimera."""
        return TextualInputGate(self, **kwargs)

    def submit_input(self, value: str) -> None:
        """Envia uma linha digitada pelo usuário para o loop do Quimera."""
        text = str(value)
        if text.strip() == CMD_EXIT:
            self.input_queue.put(CMD_EXIT)
            return
        if self.is_direct_input_active():
            self.emit(TextualUiEvent("question_clear"))
            self.direct_input_queue.put(value)
            return
        if self._try_inject_active_agent(text):
            return
        self.input_queue.put(value)

    def begin_direct_input(self) -> None:
        """Força submissões seguintes a irem para o prompt inline ativo."""
        with self._lock:
            self._direct_input_depth += 1

    def end_direct_input(self) -> None:
        """Libera roteamento direto quando o prompt inline termina."""
        with self._lock:
            self._direct_input_depth = max(0, self._direct_input_depth - 1)

    def is_direct_input_active(self) -> bool:
        """Retorna True se há prompt inline aguardando resposta."""
        with self._lock:
            return self._direct_input_depth > 0

    def set_input_value(self, value: str) -> None:
        """Atualiza snapshot thread-safe do buffer editável atual."""
        with self._lock:
            self._input_value = str(value or "")

    def get_input_value(self) -> str:
        """Retorna snapshot thread-safe do buffer editável atual."""
        with self._lock:
            return self._input_value

    def set_agent_active(self, agent: str, label: str) -> None:
        """Marca agente como ativo para estado da toolbar."""
        key = str(agent or "")
        if not key:
            return
        with self._lock:
            self._active_agent_labels[key] = str(label or key)

    def clear_agent_active(self, agent: str) -> None:
        """Remove agente ativo da toolbar."""
        key = str(agent or "")
        with self._lock:
            self._active_agent_labels.pop(key, None)

    def active_agent_label(self) -> str | None:
        """Retorna o agente ativo mais recente para exibição na toolbar."""
        with self._lock:
            if not self._active_agent_labels:
                return None
            return next(reversed(self._active_agent_labels.values()))

    def _try_inject_active_agent(self, text: str) -> bool:
        """Tenta enviar texto ao stdin do agente ativo, preservando contrato do split."""
        with self._lock:
            quimera_app = self.quimera_app
        if not bool(getattr(quimera_app, "is_agent_running", False)):
            return False
        stdin = getattr(quimera_app, "active_agent_stdin", None)
        if stdin is None:
            return False
        try:
            stdin.write(text + "\n")
            stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def emit(self, event: TextualUiEvent) -> None:
        """Envia evento visual para a UI, com fallback para fila interna."""
        with self._lock:
            textual_app = self.textual_app
        if textual_app is None:
            self.ui_queue.put(event)
            return
        try:
            textual_app.call_from_thread(textual_app.handle_bridge_event, event)
        except RuntimeError:
            self.ui_queue.put(event)

    def flush_ui_events(self) -> bool:
        """Força o app Textual a drenar eventos visuais pendentes agora."""
        with self._lock:
            textual_app = self.textual_app
        if textual_app is None:
            return False
        flush_bridge_events = getattr(textual_app, "flush_bridge_events", None)
        if not callable(flush_bridge_events):
            return False
        try:
            textual_app.call_from_thread(flush_bridge_events)
            return True
        except RuntimeError:
            return False

    def drain_pending_events(self) -> list[TextualUiEvent]:
        """Drena eventos acumulados antes da montagem do app."""
        events: list[TextualUiEvent] = []
        while True:
            try:
                events.append(self.ui_queue.get_nowait())
            except queue.Empty:
                return events

    def cancel_or_exit(self) -> None:
        """Cancela agente ativo ou solicita saída limpa."""
        with self._lock:
            quimera_app = self.quimera_app
        if bool(getattr(quimera_app, "is_agent_running", False)):
            lifecycle = getattr(quimera_app, "chat_lifecycle", None)
            handle_interrupt = getattr(lifecycle, "handle_local_interrupt", None)
            if callable(handle_interrupt):
                handle_interrupt()
                self.emit(TextualUiEvent("system", "cancelamento solicitado"))
                return
        agent_client = getattr(quimera_app, "agent_client", None)
        if bool(getattr(agent_client, "_agent_running", False)):
            cancel = getattr(agent_client, "cancel_active_work", None)
            if callable(cancel):
                cancel()
                self.emit(TextualUiEvent("system", "cancelamento solicitado"))
                return
        self.submit_input("/exit")


class TextualInputGate:
    """Input gate por fila para a TUI Textual."""

    def __init__(
        self,
        bridge: TextualUiBridge,
        renderer=None,
        toolbar_context_resolver=None,
        history_file=None,
        command_resolver=None,
        argument_resolver=None,
    ) -> None:
        self._bridge = bridge
        self._renderer = renderer
        self._toolbar_context_resolver = toolbar_context_resolver
        self._command_resolver = command_resolver
        self._argument_resolver = argument_resolver
        self._theme_cycle_handler = None
        self._history_file = history_file
        self._textual_mounted = False
        self._active_lock = threading.Lock()
        self._active = False
        self._interactive_prompt_active = False
        self._owner_thread_id: int | None = None

    def set_toolbar_context_resolver(self, resolver) -> None:
        """Define callback para contexto dinâmico da toolbar."""
        self._toolbar_context_resolver = resolver

    def set_command_resolver(self, resolver) -> None:
        """Define callback para comandos disponíveis."""
        self._command_resolver = resolver

    def set_argument_resolver(self, resolver) -> None:
        """Define callback para argumentos de comandos."""
        self._argument_resolver = resolver

    def set_theme_cycle_handler(self, handler) -> None:
        """Define callback de troca de tema."""
        self._theme_cycle_handler = handler

    def is_active(self) -> bool:
        """Indica se o loop está aguardando input humano."""
        with self._active_lock:
            return self._active or self._textual_mounted

    def set_textual_mounted(self, mounted: bool) -> None:
        """Indica que a UI Textual está pronta para receber input interativo."""
        with self._active_lock:
            self._textual_mounted = bool(mounted)

    def get_owner_thread_id(self) -> int | None:
        """Retorna o thread atualmente bloqueado aguardando input."""
        with self._active_lock:
            return self._owner_thread_id

    def run_in_terminal_message(self, callback) -> bool:
        """Compatibilidade: Textual não usa run_in_terminal."""
        return False

    def redisplay(self) -> None:
        """Atualiza toolbar/sugestões enquanto o input Textual está ativo."""
        if not self.is_active():
            return None
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": "mensagem...",
                    "toolbar": self._build_toolbar_renderable(),
                    "commands": self._commands(),
                },
            )
        )
        return None

    def get_line_buffer(self) -> str:
        """Compatibilidade com callers que consultam buffer atual."""
        return self._bridge.get_input_value()

    def clear_interactive_prompt_state(self) -> None:
        """Força limpeza visual do estado de prompt interativo."""
        self._interactive_prompt_active = False

    def _set_active_state(self, active: bool) -> None:
        with self._active_lock:
            self._active = active
            self._owner_thread_id = threading.get_ident() if active else None
        self._bridge.emit(TextualUiEvent("input_active", active))

    def _toolbar_context(self) -> dict[str, str]:
        resolver = self._toolbar_context_resolver
        if callable(resolver):
            try:
                context = dict(resolver() or {})
            except Exception:
                context = {}
        else:
            context = {}
        active_agent = self._bridge.active_agent_label()
        if active_agent and not str(context.get("active_agents", "")).strip():
            context["active_agents"] = active_agent
        return context

    @staticmethod
    def _clip_toolbar_value(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max(1, max_len - 1)].rstrip() + "…"

    def _toolbar_segments(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        context = self._toolbar_context()
        responder = str(context.get("responder", "")).strip()
        model = str(context.get("model", "")).strip()
        branch = str(context.get("branch", "")).strip()
        active_agents = str(context.get("active_agents", "")).strip()
        parallel = str(context.get("parallel", "")).strip()
        open_bugs = str(context.get("open_bugs", "")).strip()
        mode = str(context.get("mode", "")).strip()
        turns = str(context.get("turns", "")).strip()
        session_id = str(context.get("session", "")).strip()
        theme = str(context.get("theme", "")).strip()

        left: list[tuple[str, str]] = []
        if responder:
            left.append((self._clip_toolbar_value(responder, 24), "accent"))
        if model:
            left.append((self._clip_toolbar_value(model, 24), "model"))
        if branch:
            left.append((f"⎇ {self._clip_toolbar_value(branch, 20)}", "info"))
        if active_agents:
            left.append((f"⚙ {self._clip_toolbar_value(active_agents, 30)}", "info"))
        if parallel:
            left.append((f"⚡ {parallel}", "info"))

        right: list[tuple[str, str]] = []
        if open_bugs:
            right.append((f"✗ {open_bugs}", "err"))
        if turns:
            right.append((f"↺ {turns}", "dim"))
        if mode:
            right.append((f"◈ {mode}", "dim"))
        if theme:
            right.append((f"✨ {self._clip_toolbar_value(theme, 12)}", "dim"))
        if session_id:
            right.append((f"🔗 {self._clip_toolbar_value(session_id, 22)}", "dim"))
        return left, right

    def _build_toolbar_text(self) -> str:
        if self._interactive_prompt_active:
            return "Enter: confirmar  |  Ctrl+C: cancelar"
        left, right = self._toolbar_segments()
        parts = [label for label, _ in left]
        parts.extend(label for label, _ in right)
        return "  |  ".join(parts)

    def _build_toolbar_renderable(self):
        if self._interactive_prompt_active:
            return Text("Enter: confirmar  |  Ctrl+C: cancelar", style="bold yellow on #252526")
        left, right = self._toolbar_segments()
        if not left and not right:
            return Text("")
        style_by_kind = {
            "accent": "bold #5fc3ff on #3e3e3e",
            "model": "#9cdcfe on #3e3e3e",
            "info": "#d4d4d4 on #3e3e3e",
            "dim": "#9e9e9e on #3e3e3e",
            "err": "bold #fc7b5f on #3e3e3e",
        }

        def _chip_width(items: list[tuple[str, str]]) -> int:
            return sum(len(label) + 2 for label, _ in items)

        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        left_width = _chip_width(left)
        right_width = _chip_width(right)
        padding = max(1, term_w - left_width - right_width) if right else 1
        text = Text(" ", style="on #252526")
        for label, kind in left:
            text.append(f" {label} ", style=style_by_kind.get(kind, "white on #3e3e3e"))
        if right:
            text.append(" " * max(1, padding - 1), style="on #252526")
        for label, kind in right:
            text.append(f" {label} ", style=style_by_kind.get(kind, "white on #3e3e3e"))
        return text

    def _commands(self) -> list[str]:
        resolver = self._command_resolver
        if not callable(resolver):
            return []
        try:
            return sorted(set(str(item) for item in (resolver() or [])))
        except Exception:
            return []

    def completions_for(self, value: str, cursor_position: int | None = None) -> list[str]:
        """Retorna sugestões compatíveis com o autocomplete do prompt antigo."""
        if cursor_position is None:
            cursor_position = len(value)
        text_before_cursor = (value[:cursor_position] or "").lstrip()
        for prefix in ("s/", "r/"):
            if text_before_cursor.startswith(prefix):
                partial = text_before_cursor[len(prefix):]
                suggestions = self._argument_suggestions(prefix.rstrip("/"), partial)
                return [f"{prefix}{suggestion}" for suggestion in suggestions]
        if not text_before_cursor.startswith("/"):
            return []
        if " " in text_before_cursor:
            command, partial = text_before_cursor.split(" ", 1)
            return [
                f"{command} {suggestion}"
                for suggestion in self._argument_suggestions(command, partial)
            ]
        return [command for command in self._commands() if command.startswith(text_before_cursor)]

    def _argument_suggestions(self, command: str, partial: str) -> list[str]:
        resolver = self._argument_resolver
        if not callable(resolver):
            return []
        try:
            suggestions = resolver(command, partial) or []
        except Exception:
            return []
        return [str(item) for item in suggestions if str(item).startswith(partial)]

    def __call__(self, prompt: str) -> str:
        """Bloqueia o loop do Quimera até o usuário submeter uma linha na TUI."""
        self._set_active_state(True)
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": prompt,
                    "toolbar": self._build_toolbar_renderable(),
                    "commands": self._commands(),
                },
            )
        )
        try:
            return self._bridge.input_queue.get()
        finally:
            self._set_active_state(False)
            self._bridge.emit(TextualUiEvent("prompt_clear"))

    def _read_with_textual_prompt(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        question: str | None = None,
        options: list[str] | None = None,
        owner: str | None = None,
        kind: str = "input",
        title: str | None = None,
    ) -> str | None:
        """Exibe um pedido interativo no Textual e lê uma submissão do input fixo."""
        self._bridge.begin_direct_input()
        if question is not None:
            self._interactive_prompt_active = True
            self._bridge.emit(
                TextualUiEvent(
                    "question",
                    {
                        "question": question,
                        "options": list(options or []),
                        "owner": owner,
                        "kind": kind,
                        "title": title,
                    },
                )
            )
        self._set_active_state(True)
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": prompt,
                    "toolbar": self._build_toolbar_renderable(),
                    "commands": self._commands(),
                },
            )
        )
        try:
            if timeout is None:
                return self._bridge.direct_input_queue.get()
            return self._bridge.direct_input_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._set_active_state(False)
            self._bridge.end_direct_input()
            if question is not None:
                self._interactive_prompt_active = False
                self._bridge.emit(TextualUiEvent("question_clear"))
            self._bridge.emit(TextualUiEvent("prompt_clear"))

    def read_plain_input(self, prompt: str) -> str:
        """Lê uma linha simples pelo mesmo input Textual."""
        return self(prompt)

    def read_input_in_terminal(self, prompt: str, timeout: float = 300.0) -> str | None:
        """Compatibilidade: lê pelo input fixo do Textual, sem stdin direto."""
        return self._read_with_textual_prompt(prompt, timeout=timeout)

    def read_selection_in_terminal(
        self,
        question: str,
        options: list[str],
        timeout: float = 300.0,
        owner: str | None = None,
    ) -> tuple[int, str] | None:
        """Lê seleção por número/texto no input Textual."""
        raw = self._read_with_textual_prompt(
            f"Selecione (1-{len(options)}): ",
            timeout=timeout,
            question=question,
            options=options,
            owner=owner,
            kind="selection",
        )
        if raw is None:
            return None
        raw = raw.strip()
        try:
            index = int(raw) - 1
            if 0 <= index < len(options):
                return index, options[index]
        except ValueError:
            pass
        for index, option in enumerate(options):
            if option.lower() == raw.lower():
                return index, option
        return None

    def read_approval_in_terminal(
        self,
        question: str,
        prompt: str,
        timeout: float = 300.0,
        owner: str | None = None,
    ) -> str | None:
        """Lê aprovação pelo input Textual."""
        return self._read_with_textual_prompt(
            prompt,
            timeout=timeout,
            question=question,
            options=_approval_options(),
            owner=owner,
            kind="approval",
            title=_APPROVAL_TITLE,
        )


class TextualRenderer:
    """Renderer compatível com a API usada pelo Quimera, emitindo para Textual."""

    supports_agent_feed = True

    def __init__(self, bridge: TextualUiBridge) -> None:
        self._bridge = bridge
        self._audit_logger = None
        self._console = _TextualConsoleShim(bridge)
        self._profile_resolver: Callable | None = None
        self._theme = themes.get(themes.DEFAULT_THEME)
        self._statuses: dict[str, str] = {}
        self._stream_content_by_agent: dict[str, str] = {}

    @property
    def theme_name(self) -> str:
        """Retorna o nome do tema ativo."""
        return self._theme.name

    def cycle_theme(self) -> str:
        """Avança para o próximo tema compartilhado com o renderer legado."""
        all_names = themes.names()
        try:
            idx = all_names.index(self._theme.name)
        except ValueError:
            idx = 0
        next_name = all_names[(idx + 1) % len(all_names)]
        self._theme = themes.get(next_name)
        self._bridge.emit(TextualUiEvent("theme_changed", {"theme": next_name}))
        return next_name

    def set_profile_resolver(self, resolver: Callable) -> None:
        """Define callback para resolver (color, label) por agente."""
        self._profile_resolver = resolver

    def _resolve_agent_label(self, agent: str) -> str:
        """Retorna label formatada com ícone do agente, ex: '🔮  Claude'."""
        _style, label = self._resolve_agent_style(agent)
        return label

    def _resolve_agent_style(self, agent: str) -> tuple[str, str]:
        """Retorna (style, label) para o agente usando o resolver existente."""
        resolver = self._profile_resolver
        if resolver:
            try:
                result = resolver(str(agent).lower())
                if result:
                    style, label = result
                    return str(style or "cyan"), str(label)
            except Exception:
                pass
        agent_name = str(agent).capitalize() if agent else "Agente"
        return "cyan", f"🤖  {agent_name}"

    def _agent_event_payload(
        self,
        agent,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Monta payload visual comum para eventos de agente."""
        style, label = self._resolve_agent_style(str(agent or ""))
        payload = {"label": label, "style": style, "theme": self._theme.name}
        if extra:
            payload.update(extra)
        return payload

    def set_prompt_integration(self, is_active_fn, run_above_fn) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def close(self, timeout: float = 5.0) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def log_debug_event(self, event: str, **payload) -> None:
        """Compatibilidade com auditoria do TerminalRenderer."""
        if self._audit_logger is None:
            return
        try:
            self._audit_logger.log_event(event, **payload)
        except Exception:
            return

    @contextmanager
    def terminal_floor(self, *, title: str = "Terminal floor", metadata: dict[str, Any] | None = None, timeout: float = 2.0):
        """Compatibilidade para I/O baixo nível que pede posse do terminal."""
        with self._interactive_window("terminal_floor", title, metadata=metadata):
            yield

    def external_window(self, window_id: str, title: str = "", metadata=None):
        """Entrega temporariamente o terminal para uma janela/processo externo."""
        with self._bridge._lock:
            textual_app = self._bridge.textual_app
        return _external_textual_window(textual_app)

    @contextmanager
    def _interactive_window(self, kind: str, title: str, owner: str | None = None, metadata=None):
        """Sinaliza janela interativa sem ceder stdout fora do Textual."""
        metadata_dict = dict(metadata or {})
        options = _approval_options() if kind == "approval" else []
        question = str(metadata_dict.get("question") or "")
        should_show_overlay = kind == "approval" or bool(question) or bool(options)
        self._bridge.begin_direct_input()
        if should_show_overlay:
            self._bridge.emit(
                TextualUiEvent(
                    "window_open",
                    {
                        "kind": kind,
                        "title": title,
                        "owner": owner,
                        "metadata": metadata_dict,
                        "question": question,
                        "options": options,
                    },
                )
            )
        try:
            yield
        finally:
            if should_show_overlay:
                self._bridge.emit(TextualUiEvent("window_clear", {"kind": kind}))
            self._bridge.end_direct_input()

    def approval_window(self, *, title: str = "Permissão solicitada", owner: str | None = None, metadata=None, **kwargs):
        """Compatibilidade com fluxos legados de aprovação."""
        return self._interactive_window("approval", title, owner=owner, metadata=metadata)

    def input_window(self, *, title: str = "Entrada solicitada", owner: str | None = None, metadata=None, **kwargs):
        """Compatibilidade com fluxos legados de entrada."""
        return self._interactive_window("input", title, owner=owner, metadata=metadata)

    def selection_window(self, *, title: str = "Seleção solicitada", owner: str | None = None, metadata=None, **kwargs):
        """Compatibilidade com fluxos legados de seleção."""
        return self._interactive_window("selection", title, owner=owner, metadata=metadata)

    def flush(self, timeout: float = 5.0) -> None:
        """Drena eventos visuais pendentes no app Textual."""
        self._bridge.flush_ui_events()

    def flush_quick(self, timeout: float = 0.15) -> bool:
        """Drena eventos visuais pendentes sem bloquear o prompt."""
        return self._bridge.flush_ui_events()

    def show_system(self, message: str) -> None:
        """Exibe mensagem de sistema."""
        self._bridge.emit(TextualUiEvent("system", str(message)))

    def show_banner(self, message: str) -> None:
        """Exibe banner de boas-vindas/logo no feed Textual."""
        self._bridge.emit(TextualUiEvent("banner", strip_ansi(str(message)).strip("\r\n")))

    def show_system_neutral(self, message: str) -> None:
        """Exibe mensagem neutra."""
        self._bridge.emit(TextualUiEvent("muted", str(message)))

    def show_warning(self, message: str) -> None:
        """Exibe warning."""
        self._bridge.emit(TextualUiEvent("warning", str(message)))

    def show_error(self, message: str, **metadata) -> None:
        """Exibe erro."""
        agent = metadata.get("agent")
        command_name = metadata.get("command_name")
        error_kind = metadata.get("error_kind")
        return_code = metadata.get("return_code")
        clean_message = strip_ansi(str(message)).strip("\r\n")
        subject = str(agent or command_name or "").strip()
        if error_kind == "agent_exit" and return_code is not None:
            clean_message = (
                f"[erro] retornou código {return_code}"
                if agent
                else f"[erro] agente {subject or 'unknown'} retornou código {return_code}"
            )
        elif error_kind == "agent_comm":
            clean_message = (
                f"[erro] falha ao comunicar: {clean_message}"
                if agent
                else f"[erro] falha ao comunicar com {subject or 'unknown'}: {clean_message}"
            )
        elif error_kind == "agent_invalid_output":
            clean_message = (
                "[erro] não retornou saída válida"
                if agent
                else f"[erro] agente {subject or 'unknown'} não retornou saída válida"
            )
        self._bridge.emit(TextualUiEvent("error", clean_message, agent=str(agent) if agent else None))

    def show_approval(self, message: str) -> None:
        """Exibe bloco persistente de aprovação no feed."""
        self._bridge.emit(TextualUiEvent("approval", strip_ansi(str(message)).strip("\r\n")))

    def clear_screen(self) -> None:
        """Limpa o feed Textual sem escrever ANSI direto no terminal."""
        self._bridge.emit(TextualUiEvent("clear"))

    def show_plain(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto simples."""
        kind = "muted" if muted else "plain"
        self._bridge.emit(TextualUiEvent(kind, str(message), agent=agent))

    def show_feed(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto no feed."""
        self.show_plain(message, agent=agent, muted=muted)

    def show_turn_summary(self, agent: str | None, detail: dict) -> None:
        """Exibe resumo compacto de tools do turno."""
        runtime = str((detail or {}).get("runtime") or "").strip().lower()
        if runtime and runtime != "cli":
            return
        tools = detail.get("tools", []) if isinstance(detail, dict) else []
        if not isinstance(tools, list) or not tools:
            return
        total = 0
        ok_count = 0
        err_count = 0
        total_ms = 0
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            total += 1
            status = str(tool.get("status") or "").strip().lower()
            if status in {"ok", "success", "succeeded"}:
                ok_count += 1
            if status in {"error", "failed", "fail", "timeout"}:
                err_count += 1
            duration_ms = tool.get("duration_ms")
            if isinstance(duration_ms, int) and duration_ms >= 0:
                total_ms += duration_ms
        if total <= 0:
            return
        duration = f"{total_ms}ms" if total_ms < 1000 else f"{total_ms / 1000:.1f}s"
        summary = f"TOOLS: {total} chamadas · {ok_count} ok · {err_count} erro · {duration}"
        self._bridge.emit(TextualUiEvent("turn_summary", summary, agent=agent))

    def show_delegation(self, from_agent, to_agent, task=None) -> None:
        """Exibe delegação entre agentes."""
        from_style, from_label = self._resolve_agent_style(str(from_agent))
        to_style, to_label = self._resolve_agent_style(str(to_agent))
        self._bridge.emit(
            TextualUiEvent(
                "delegation",
                {
                    "from_label": from_label,
                    "from_style": from_style,
                    "to_label": to_label,
                    "to_style": to_style,
                    "task": str(task or "").strip(),
                },
            )
        )

    def show_agent_lifecycle(self, agent: str, status: str, message: str) -> None:
        """Exibe lifecycle transitório de agente como evento semântico."""
        self._bridge.emit(
            TextualUiEvent(
                "agent_lifecycle",
                {"status": str(status), "message": str(message)},
                agent=str(agent),
            )
        )

    def show_message(self, agent, content, render_mode: str = "auto") -> None:
        """Exibe resposta final de agente com ícone."""
        clean_content = strip_ansi(_extract_text_from_renderable(content))
        self._stream_content_by_agent.pop(str(agent), None)
        self._bridge.clear_agent_active(str(agent))
        self._bridge.emit(
            TextualUiEvent(
                "agent_message",
                self._agent_event_payload(
                    agent,
                    {"content": clean_content, "render_mode": render_mode},
                ),
                agent=str(agent),
            )
        )

    def show_no_response(self, agent) -> None:
        """Exibe ausência de resposta."""
        self.show_message(agent, _NO_RESPONSE_MESSAGE, render_mode="plain")

    def start_message_stream(self, agent) -> None:
        """Inicia stream visual com ícone do agente."""
        label = self._resolve_agent_label(agent)
        self._bridge.set_agent_active(str(agent), label)
        self._stream_content_by_agent[str(agent)] = ""
        self._bridge.emit(
            TextualUiEvent("stream_start", self._agent_event_payload(agent), agent=str(agent))
        )

    def update_message_stream(self, agent, chunk) -> None:
        """Atualiza stream visual."""
        agent_key = str(agent)
        current = self._stream_content_by_agent.get(agent_key, "")
        if isinstance(chunk, dict):
            diff = _normalize_stream_diff(chunk.get("diff"))
            if diff:
                current = _apply_stream_diff(current, diff)
            elif chunk.get("text"):
                current += strip_ansi(str(chunk.get("text")))
            else:
                current += strip_ansi(str(chunk))
        else:
            current += strip_ansi(str(chunk))
        self._stream_content_by_agent[agent_key] = current
        self._bridge.emit(TextualUiEvent("stream_chunk", chunk, agent=str(agent)))

    def finish_message_stream(
        self,
        agent,
        final_content: str,
        render_mode: str = "auto",
    ) -> None:
        """Finaliza stream visual com ícone do agente."""
        self.show_message(agent, final_content, render_mode=render_mode)

    def commit_agent_stream(self, agent, render_mode: str = "auto") -> bool:
        """Compatibilidade com TerminalRenderer."""
        agent_key = str(agent)
        content = self._stream_content_by_agent.get(agent_key, "")
        if not str(content or "").strip():
            return False
        self.show_message(agent_key, content, render_mode=render_mode)
        return True

    def abort_message_stream(self, agent) -> None:
        """Aborta stream visual."""
        self._stream_content_by_agent.pop(str(agent), None)
        self._bridge.clear_agent_active(str(agent))
        self._bridge.emit(
            TextualUiEvent("stream_abort", self._agent_event_payload(agent), agent=str(agent))
        )

    def update_agent_transient(self, agent, message: str) -> None:
        """Exibe progresso transitório como linha de status."""
        self._bridge.emit(TextualUiEvent("agent_update", str(message), agent=str(agent)))

    def clear_agent_transient(self, agent) -> None:
        """Compatibilidade com TerminalRenderer."""
        self._bridge.emit(TextualUiEvent("visual_reset", agent=str(agent)))

    def reset_visual_state(self, agent: str | None = None) -> None:
        """Limpa estados visuais transitórios após cancelamento."""
        if agent:
            self._bridge.clear_agent_active(str(agent))
            self._bridge.emit(TextualUiEvent("visual_reset", agent=str(agent)))
            return
        self._statuses.clear()
        self._bridge.emit(TextualUiEvent("visual_reset"))

    def set_agent_pending_input(self, agent: str, kind: str, question: str = "") -> None:
        """Sinaliza input pendente de agente como card visual."""
        label = "aprovação pendente" if str(kind) == "approval" else "input pendente"
        first_line = str(question or label).strip().splitlines()[0] if str(question or "").strip() else label
        payload = self._agent_event_payload(
            agent,
            {"kind": str(kind or "input"), "question": str(question or first_line)},
        )
        self._bridge.emit(TextualUiEvent("pending_input", payload, agent=str(agent)))

    def clear_agent_pending_input(self, agent: str) -> None:
        """Remove status transitório de input pendente."""
        self.clear_agent_transient(agent)

    def update_status(self, agent, message) -> None:
        """Atualiza status de agente paralelo no feed Textual."""
        key = str(agent or "global")
        self._statuses[key] = str(message)
        self._bridge.emit(TextualUiEvent("agent_lifecycle", {"status": "running", "message": str(message)}, agent=key))

    @contextmanager
    def live_status(self, agents):
        """Context manager de status para múltiplos agentes."""
        for agent in agents or []:
            self.update_status(agent, "inicializando...")
        try:
            yield
        finally:
            for agent in agents or []:
                self.update_status(agent, "concluído")

    def running_status(self, initial="", agent=None):
        """Retorna context manager compatível com status Rich."""
        return _TextualStatus(self, str(agent) if agent else None, str(initial or ""))

    def show_newline(self) -> None:
        """Exibe linha vazia."""
        self._bridge.emit(TextualUiEvent("plain", ""))

    def show_prompt_preview(self, agent: str, preview: str) -> None:
        """Exibe preview de prompt."""
        self._bridge.emit(TextualUiEvent("plain", preview, agent=agent))

    def set_summarizing(self, active: bool) -> None:
        """Sinaliza início/fim de sumarização para animação no header."""
        self._bridge.emit(TextualUiEvent("summarizing", active))


def _render_event(event: TextualUiEvent):
    """Converte eventos do bridge para renderables Rich."""
    if event.kind == "agent_message":
        payload = event.payload or {}
        content = str(payload.get("content", ""))
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _render_turn_block(
            theme_name,
            label,
            style,
            content=content,
            render_mode=str(payload.get("render_mode") or "auto"),
        )
    if event.kind == "stream_start":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, "gerando...")
    if event.kind == "stream_abort":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "red") or "red")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, "interrompido")
    if event.kind == "stream_chunk":
        payload = event.payload if isinstance(event.payload, dict) else {}
        content = str(payload.get("content") or payload.get("text") or event.payload)
        if not content.strip():
            return None
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        theme_name = str(payload.get("theme", themes.DEFAULT_THEME) or themes.DEFAULT_THEME)
        return _build_stream_renderable(theme_name, label, style, content)
    if event.kind == "pending_input":
        payload = event.payload if isinstance(event.payload, dict) else {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        style = str(payload.get("style", "cyan") or "cyan")
        question = str(payload.get("question") or "")
        kind = str(payload.get("kind") or "input")
        return _build_pending_card_renderable(label, style, question, kind=kind)
    if event.kind == "agent_lifecycle":
        payload = event.payload or {}
        message = str(payload.get("message", "")) if isinstance(payload, dict) else str(payload)
        if not message.strip():
            return None
        agent = event.agent or "agente"
        return Panel(Text(message, style="dim"), title=f"🤖 {agent}", border_style="dim")
    if event.kind in {"warning", "error"}:
        style = "yellow" if event.kind == "warning" else "red"
        return Text(str(event.payload), style=style)
    if event.kind == "banner":
        return Group(Text(str(event.payload), style="bold cyan"), Rule(style="dim cyan"))
    if event.kind == "approval":
        lines = str(event.payload or "").splitlines()
        title = lines[0] if lines else "Permissão solicitada"
        body = "\n".join(lines[1:]) if len(lines) > 1 else str(event.payload or "")
        return Panel(Text(body, style="yellow"), title=f"[bold yellow]{title}[/bold yellow]", border_style="yellow")
    if event.kind == "delegation":
        payload = event.payload if isinstance(event.payload, dict) else {}
        task = str(payload.get("task", "")).strip()
        text = Text()
        text.append(str(payload.get("from_label", "agente")), style=f"bold {payload.get('from_style', 'cyan')}")
        text.append(" → ", style="dim")
        text.append(str(payload.get("to_label", "agente")), style=f"bold {payload.get('to_style', 'cyan')}")
        if task:
            text.append(f" · {task}", style="dim")
        return Rule(text, style="dim")
    if event.kind == "turn_summary":
        prefix = f"{event.agent} " if event.agent else ""
        return Text(f"{prefix}{event.payload}", style="dim")
    if event.kind == "question":
        payload = event.payload or {}
        lines = [str(payload.get("question", ""))]
        for index, option in enumerate(payload.get("options", []) or [], 1):
            lines.append(f"{index}. {option}")
        return Panel("\n".join(lines), title="input solicitado", border_style="yellow")
    if event.kind == "agent_update":
        prefix = f"{event.agent}: " if event.agent else ""
        return Text(f"{prefix}{event.payload}", style="dim")
    if event.kind == "prompt":
        return None
    if event.kind == "input_active":
        return None
    if event.kind == "clear":
        return None
    if event.kind == "muted":
        return Text(str(event.payload), style="dim")
    if event.kind == "system":
        return Text(str(event.payload), style="blue")
    if event.kind == "theme_changed":
        payload = event.payload if isinstance(event.payload, dict) else {}
        theme_name = str(payload.get("theme", "")).strip()
        return Text(f"tema: {theme_name}" if theme_name else "tema atualizado", style="dim cyan")
    return Text(str(event.payload))


def _render_themed_agent_block(theme_name: str, label: str, style: str, body, *, streaming: bool = False):
    """Renderiza bloco de agente na Textual usando os mesmos nomes de tema do renderer legado."""
    name = themes.get(theme_name).name
    if name == "panel":
        title = f"[bold {style}]{label}[/bold {style}]"
        return Panel(body, title=title, border_style=style, padding=(0, 1))
    if name == "chat":
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=2)
        table.add_column(ratio=1)
        table.add_row(
            Text("●", style=f"bold {style}"),
            Group(
                Text(label, style=f"bold {style}"),
                Padding(body, pad=(0, 0, 0, 2)),
            ),
        )
        return table
    if name == "rule":
        return Group(
            Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}"),
            body,
            Rule(style="dim"),
        )
    if name == "minimal":
        return Group(Text(f"▶ {label}", style=f"bold {style}"), Padding(body, pad=(0, 0, 0, 2)))
    if name == "card":
        return Panel(
            body,
            title=f"[bold {style}]{label}[/bold {style}]",
            border_style=f"dim {style}",
            padding=(0, 1),
            subtitle="▸" if not streaming else None,
            subtitle_align="right",
        )
    if name == "line":
        return Group(Text(label, style=f"bold {style}"), body)
    return Panel(body, title=label, border_style=style)


def _build_turn_header(theme_name: str, label: str, style: str):
    """Monta cabeçalho de turno seguindo o renderer main-tui."""
    name = themes.get(theme_name).name
    if name == "chat":
        header = Table.grid(expand=True, padding=(0, 1))
        header.add_column(width=2)
        header.add_column(ratio=1)
        header.add_row(Text("●", style=f"bold {style}"), Text(label, style=f"bold {style}"))
        return header
    if name == "rule":
        return Rule(f"[bold {style}]{label}[/bold {style}]", style=f"dim {style}")
    if name == "minimal":
        return Text(f"▶ {label}", style=f"bold {style}")
    if name == "card":
        return Text(f"▎ {label}", style=f"bold {style}")
    if name == "line":
        return Text(label, style=f"bold {style}")
    return Text(label, style=f"bold {style}")


def _build_turn_body(
    theme_name: str,
    label: str,
    style: str,
    content: str,
    *,
    streaming: bool = False,
    render_mode: str = "auto",
):
    """Monta corpo textual do turno seguindo o renderer main-tui."""
    name = themes.get(theme_name).name
    mode = str(render_mode or "auto").strip().lower()
    if mode == "auto":
        mode = "markdown"
    body_content = Text(content or "", no_wrap=False, overflow="fold") if streaming or mode == "plain" else Markdown(content or "")
    if name == "panel":
        title = f"[bold {style}]{label}[/bold {style}]" if streaming else None
        return Panel(body_content, title=title, border_style=style, padding=(0, 1))
    if name == "chat":
        return Padding(body_content, pad=(0, 0, 0, 4))
    if name == "minimal":
        return Padding(body_content, pad=(0, 0, 0, 2))
    if name == "card":
        return Panel(body_content, border_style=f"dim {style}", padding=(0, 1))
    if name == "line":
        return body_content
    return body_content


def _render_turn_block(
    theme_name: str,
    label: str,
    style: str,
    *,
    content: str | None = None,
    tools_table=None,
    turn_id: str = "",
    include_header: bool = True,
    include_footer_rule: bool = False,
    streaming: bool = False,
    render_mode: str = "auto",
):
    """Monta bloco estruturado de turno: header -> corpo -> tools."""
    parts = []
    if include_header:
        parts.append(_build_turn_header(theme_name, label, style))
    if content:
        parts.append(
            _build_turn_body(
                theme_name,
                label,
                style,
                content,
                streaming=streaming,
                render_mode=render_mode,
            )
        )
    if tools_table is not None:
        parts.append(_build_turn_tools(theme_name, label, style, tools_table, turn_id))
    if include_footer_rule and themes.get(theme_name).name == "rule":
        parts.append(Rule(style="dim"))
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return Group(*parts)


def _build_turn_tools(theme_name: str, label: str, style: str, tools_table, turn_id: str):
    """Monta seção de tools vinculada visualmente ao turno."""
    name = themes.get(theme_name).name
    title = f"tools · {turn_id}" if turn_id else "tools"
    if name == "panel":
        return Panel(
            tools_table,
            title=f"[bold {style}]{label} · {title}[/bold {style}]",
            border_style=style,
            padding=(0, 0),
        )
    if name == "chat":
        row = Table.grid(expand=True, padding=(0, 1))
        row.add_column(width=2)
        row.add_column(ratio=1)
        row.add_row(
            Text("◦", style=f"dim {style}"),
            Group(Text(title, style=f"bold {style}"), Padding(tools_table, pad=(0, 0, 0, 2))),
        )
        return row
    if name == "rule":
        return Group(Text(title, style=f"bold {style}"), tools_table)
    if name == "minimal":
        return Group(Text(f"◦ {title}", style=f"bold {style}"), Padding(tools_table, pad=(0, 0, 0, 2)))
    if name == "card":
        return Panel(
            tools_table,
            border_style=f"dim {style}",
            padding=(0, 1),
            title=f"[bold {style}]{title}[/bold {style}]" if turn_id else None,
        )
    if name == "line":
        return Group(Text(title, style=f"bold {style}"), tools_table)
    return tools_table


def _build_stream_renderable(theme_name: str, label: str, style: str, content: str):
    """Monta o renderable dinâmico usado no streaming."""
    return _render_turn_block(
        theme_name,
        label,
        style,
        content=content,
        include_header=True,
        streaming=True,
        render_mode="plain",
    )


def _build_pending_card_renderable(label: str, style: str, question: str, *, kind: str = "input"):
    """Monta badge inline de aprovação/input pendente."""
    icon = "⚠" if str(kind).strip().lower() == "approval" else "❓"
    fallback = "aguardando aprovação" if icon == "⚠" else "aguardando input"
    first_line = str(question or "").strip().splitlines()[0] if str(question or "").strip() else fallback
    content = Text.assemble(
        (f"\n{icon} ", "bold yellow"),
        (first_line, "bold yellow"),
        ("\n  Executar? [y/N/a=todas]\n" if icon == "⚠" else "\n  aguardando resposta do usuário\n", "dim yellow"),
    )
    return Panel(
        Padding(content, pad=(0, 0, 0, 2)),
        title=f"[bold {style}]{label}[/bold {style}] · pendente",
        border_style="yellow",
        padding=(0, 1),
    )


def run_textual_quimera_app(quimera_app, bridge: TextualUiBridge) -> None:
    """Executa a interface Textual como UI principal do Quimera."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Vertical
        from textual.widgets import Header, Input, RichLog, Static
        from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderIcon, HeaderTitle

        from quimera.app.completion_dropdown import CompletionDropdown, PromptHistorySuggester
    except ImportError as exc:
        raise SystemExit(
            "A interface Textual requer a dependência 'textual'. "
            "Reinstale com: pip install -e ."
        ) from exc

    _post_exit_messages: list[tuple[str, str]] = []

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

    class QuimeraTextualApp(App):
        """TUI principal do Quimera."""

        CSS = """
        Screen {
            layout: vertical;
            background: $surface;
        }
        #main {
            height: 1fr;
            min-height: 14;
        }
        #feed {
            height: 1fr;
            min-height: 10;
            padding: 0 1;
            background: $background;
        }
        #toolbar {
            height: 1;
            padding: 0 1;
            color: $text;
            background: #252526;
        }
        #question_overlay {
            display: none;
            height: auto;
            max-height: 6;
            padding: 0 1;
            background: $surface;
        }
        #input_bar {
            height: 3;
            max-height: 3;
            padding: 0 1;
            background: $surface;
        }
        #input {
            width: 100%;
        }
        #summary-spinner {
            dock: right;
            width: 3;
            color: $warning;
            content-align: center middle;
        }
        HeaderClock {
            width: 10;
            padding: 0 1;
        }
        """

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
                with Vertical(id="input_bar"):
                    yield _CompletionInput(placeholder="mensagem...", id="input")

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
            value = event.value
            event.input.value = ""
            bridge.set_input_value("")
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
            input_widget = self.query_one("#input", Input)
            input_widget.placeholder = "mensagem..."
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
            value = str(event.value)
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
                input_widget = self.query_one("#input", Input)
                prompt = str(payload.get("prompt") or "mensagem...").strip()
                input_widget.placeholder = prompt or "mensagem..."
                input_widget.focus()
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
        QuimeraTextualApp().run()
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
