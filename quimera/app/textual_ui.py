"""Interface Textual principal do Quimera."""
from __future__ import annotations

import logging
import queue
import sys
import traceback
import threading
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from quimera.ui.text import (
    _apply_stream_diff,
    _extract_text_from_renderable,
    _normalize_stream_diff,
    strip_ansi,
)
from quimera.app.config import handler as _screen_handler

_NO_RESPONSE_MESSAGE = "sem resposta válida"
_SUMMARY_SPINNER_FRAMES = ("◐", "◓", "◑", "◒")


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


class TextualFeedModel:
    """Modelo testável do feed: transitórios por agente são substituíveis."""

    _TRANSIENT_KINDS = {"stream_start", "stream_chunk", "stream_abort", "agent_update", "agent_lifecycle"}

    _IGNORED_KINDS = {"prompt", "input_active", "summarizing"}

    def __init__(self) -> None:
        self._items: list[TextualFeedItem] = []
        self._transient_index_by_agent: dict[str, int] = {}
        self._stream_buffer_by_agent: dict[str, str] = {}
        self._finalized_agents: set[str] = set()

    @property
    def items(self) -> list[TextualFeedItem]:
        """Snapshot dos itens atuais do feed."""
        return list(self._items)

    def clear(self) -> None:
        """Limpa estado do feed."""
        self._items.clear()
        self._transient_index_by_agent.clear()
        self._stream_buffer_by_agent.clear()
        self._finalized_agents.clear()

    def apply(self, event: TextualUiEvent) -> bool:
        """Aplica evento e retorna se o feed visual precisa ser redesenhado."""
        if event.kind in self._IGNORED_KINDS:
            return False
        if event.kind == "agent_message":
            self._replace_transient_with_final(event)
            return True
        if event.kind == "stream_start":
            agent = self._agent_key(event)
            self._finalized_agents.discard(agent)
            self._stream_buffer_by_agent[agent] = ""
            self._upsert_transient(event)
            return True
        if event.kind == "stream_chunk":
            self._apply_stream_chunk(event)
            return True
        if event.kind in self._TRANSIENT_KINDS:
            if self._is_late_completed_lifecycle(event):
                return False
            self._upsert_transient(event)
            return True
        self._items.append(TextualFeedItem(event, transient=False))
        return True

    def _agent_key(self, event: TextualUiEvent) -> str:
        return str(event.agent or "__global__")

    def _upsert_transient(self, event: TextualUiEvent) -> None:
        agent = self._agent_key(event)
        item = TextualFeedItem(event, transient=True)
        index = self._transient_index_by_agent.get(agent)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return
        self._transient_index_by_agent[agent] = len(self._items)
        self._items.append(item)

    def _replace_transient_with_final(self, event: TextualUiEvent) -> None:
        agent = self._agent_key(event)
        self._stream_buffer_by_agent.pop(agent, None)
        self._finalized_agents.add(agent)
        item = TextualFeedItem(event, transient=False)
        index = self._transient_index_by_agent.pop(agent, None)
        if index is not None and 0 <= index < len(self._items):
            self._items[index] = item
            return
        self._items.append(item)

    def _is_late_completed_lifecycle(self, event: TextualUiEvent) -> bool:
        if event.kind != "agent_lifecycle":
            return False
        agent = self._agent_key(event)
        if agent not in self._finalized_agents:
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("status", "")).strip().lower() == "completed"

    def _apply_stream_chunk(self, event: TextualUiEvent) -> None:
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
            self._upsert_transient(TextualUiEvent("stream_chunk", current, agent=event.agent))


def _resolve_textual_feed_limit(quimera_app) -> int | None:
    """Resolve o limite de linhas do feed a partir da config já carregada no app."""
    auto_summarize_threshold = getattr(quimera_app, "auto_summarize_threshold", None)
    if isinstance(auto_summarize_threshold, int) and auto_summarize_threshold > 0:
        return auto_summarize_threshold
    prompt_builder = getattr(quimera_app, "prompt_builder", None)
    history_window = getattr(prompt_builder, "history_window", None) if prompt_builder else None
    if isinstance(history_window, int) and history_window > 0:
        return history_window
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
        self.ui_queue: queue.Queue[TextualUiEvent] = queue.Queue()
        self.textual_app = None
        self.quimera_app = None
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
        self.input_queue.put(value)

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
                    "toolbar": self._build_toolbar_text(),
                    "commands": self._commands(),
                },
            )
        )
        return None

    def get_line_buffer(self) -> str:
        """Compatibilidade com callers que consultam buffer atual."""
        return ""

    def _set_active_state(self, active: bool) -> None:
        with self._active_lock:
            self._active = active
            self._owner_thread_id = threading.get_ident() if active else None
        self._bridge.emit(TextualUiEvent("input_active", active))

    def _build_toolbar_text(self) -> str:
        resolver = self._toolbar_context_resolver
        if not callable(resolver):
            return ""
        try:
            context = resolver() or {}
        except Exception:
            return ""
        parts = []
        for key in (
            "responder",
            "model",
            "branch",
            "active_agents",
            "parallel",
            "open_bugs",
            "mode",
            "turns",
            "session",
            "theme",
        ):
            value = str(context.get(key, "")).strip()
            if value:
                parts.append(value)
        return "  |  ".join(parts)

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
                    "toolbar": self._build_toolbar_text(),
                    "commands": self._commands(),
                },
            )
        )
        try:
            return self._bridge.input_queue.get()
        finally:
            self._set_active_state(False)

    def _read_with_textual_prompt(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        question: str | None = None,
        options: list[str] | None = None,
        owner: str | None = None,
    ) -> str | None:
        """Exibe um pedido interativo no Textual e lê uma submissão do input fixo."""
        if question is not None:
            self._bridge.emit(
                TextualUiEvent(
                    "question",
                    {
                        "question": question,
                        "options": list(options or []),
                        "owner": owner,
                    },
                )
            )
        self._set_active_state(True)
        self._bridge.emit(
            TextualUiEvent(
                "prompt",
                {
                    "prompt": prompt,
                    "toolbar": self._build_toolbar_text(),
                    "commands": self._commands(),
                },
            )
        )
        try:
            if timeout is None:
                return self._bridge.input_queue.get()
            return self._bridge.input_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._set_active_state(False)

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
            owner=owner,
        )


class TextualRenderer:
    """Renderer compatível com a API usada pelo Quimera, emitindo para Textual."""

    supports_agent_feed = True

    def __init__(self, bridge: TextualUiBridge) -> None:
        self._bridge = bridge
        self._audit_logger = None
        self._console = _TextualConsoleShim(bridge)
        self._profile_resolver: Callable | None = None

    def set_profile_resolver(self, resolver: Callable) -> None:
        """Define callback para resolver (color, label) por agente."""
        self._profile_resolver = resolver

    def _resolve_agent_label(self, agent: str) -> str:
        """Retorna label formatada com ícone do agente, ex: '🔮  Claude'."""
        resolver = self._profile_resolver
        if resolver:
            try:
                result = resolver(str(agent).lower())
                if result:
                    _, label = result
                    return label
            except Exception:
                pass
        agent_name = str(agent).capitalize() if agent else "Agente"
        return f"🤖  {agent_name}"

    def set_prompt_integration(self, is_active_fn, run_above_fn) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def close(self, timeout: float = 5.0) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def external_window(self, window_id: str, title: str = "", metadata=None):
        """Entrega temporariamente o terminal para uma janela/processo externo."""
        with self._bridge._lock:
            textual_app = self._bridge.textual_app
        suspend = getattr(textual_app, "suspend", None)
        if callable(suspend):
            return suspend()
        return nullcontext()

    def approval_window(self, *, title: str = "Aprovação", **kwargs):
        """Compatibilidade com fluxos legados de aprovação."""
        self._bridge.emit(TextualUiEvent("system", title))
        return nullcontext()

    def input_window(self, *, title: str = "Entrada", **kwargs):
        """Compatibilidade com fluxos legados de entrada."""
        self._bridge.emit(TextualUiEvent("system", title))
        return nullcontext()

    def selection_window(self, *, title: str = "Seleção", **kwargs):
        """Compatibilidade com fluxos legados de seleção."""
        self._bridge.emit(TextualUiEvent("system", title))
        return nullcontext()

    def flush(self, timeout: float = 5.0) -> None:
        """Textual processa eventos pelo próprio loop."""
        return None

    def flush_quick(self, timeout: float = 0.15) -> bool:
        """Textual processa eventos pelo próprio loop."""
        return True

    def show_system(self, message: str) -> None:
        """Exibe mensagem de sistema."""
        self._bridge.emit(TextualUiEvent("system", str(message)))

    def show_system_neutral(self, message: str) -> None:
        """Exibe mensagem neutra."""
        self._bridge.emit(TextualUiEvent("muted", str(message)))

    def show_warning(self, message: str) -> None:
        """Exibe warning."""
        self._bridge.emit(TextualUiEvent("warning", str(message)))

    def show_error(self, message: str, **metadata) -> None:
        """Exibe erro."""
        self._bridge.emit(TextualUiEvent("error", str(message)))

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
        label = self._resolve_agent_label(agent)
        self._bridge.emit(
            TextualUiEvent(
                "agent_message",
                {"content": clean_content, "render_mode": render_mode, "label": label},
                agent=str(agent),
            )
        )

    def show_no_response(self, agent) -> None:
        """Exibe ausência de resposta."""
        self.show_message(agent, _NO_RESPONSE_MESSAGE, render_mode="plain")

    def start_message_stream(self, agent) -> None:
        """Inicia stream visual com ícone do agente."""
        label = self._resolve_agent_label(agent)
        self._bridge.emit(
            TextualUiEvent("stream_start", {"label": label}, agent=str(agent))
        )

    def update_message_stream(self, agent, chunk) -> None:
        """Atualiza stream visual."""
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
        return False

    def abort_message_stream(self, agent) -> None:
        """Aborta stream visual."""
        label = self._resolve_agent_label(agent)
        self._bridge.emit(
            TextualUiEvent("stream_abort", {"label": label}, agent=str(agent))
        )

    def update_agent_transient(self, agent, message: str) -> None:
        """Exibe progresso transitório como linha de status."""
        self._bridge.emit(TextualUiEvent("agent_update", str(message), agent=str(agent)))

    def clear_agent_transient(self, agent) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

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
        body = Markdown(content) if payload.get("render_mode") != "plain" else Text(content)
        return Panel(body, title=label, border_style="cyan")
    if event.kind == "stream_start":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        return Panel(Text("gerando...", style="dim italic"), title=label, border_style="dim")
    if event.kind == "stream_abort":
        payload = event.payload or {}
        label = str(payload.get("label", f"🤖 {event.agent or 'agente'}"))
        return Panel(Text("interrompido", style="dim red"), title=label, border_style="dim")
    if event.kind == "stream_chunk":
        content = str(event.payload) if not isinstance(event.payload, dict) else str(event.payload.get("text", event.payload))
        if not content.strip():
            return None
        agent = event.agent or "agente"
        return Panel(Text(content), title=f"🤖 {agent} (stream)", border_style="dim")
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
    return Text(str(event.payload))


def run_textual_quimera_app(quimera_app, bridge: TextualUiBridge) -> None:
    """Executa a interface Textual como UI principal do Quimera."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Vertical
        from textual.widgets import Header, Input, RichLog, Static
        from textual.widgets._header import HeaderClock, HeaderClockSpace, HeaderIcon, HeaderTitle

        from quimera.app.completion_dropdown import CompletionDropdown
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
        }
        #feed {
            height: 1fr;
            padding: 0 1;
            background: $background;
        }
        #toolbar {
            height: 1;
            padding: 0 1;
            color: $text-muted;
            background: $surface;
        }
        #input_bar {
            height: 3;
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
            ("f6", "cycle_theme", "Tema"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._worker_thread: threading.Thread | None = None
            self._commands: list[str] = []
            self._summarizing = False
            self._spinner_index = 0
            self._spinner_timer = None
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
                yield CompletionDropdown()
                with Vertical(id="input_bar"):
                    yield _CompletionInput(placeholder="mensagem...", id="input")

        def on_mount(self) -> None:
            bridge.attach_textual_app(self)
            gate = getattr(quimera_app, "input_gate", None)
            if hasattr(gate, "set_textual_mounted"):
                gate.set_textual_mounted(True)
            for event in bridge.drain_pending_events():
                self.handle_bridge_event(event)
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
            try:
                self.query_one("#input", _CompletionInput).save_history(self._history_file_path)
            except Exception:
                pass

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
            event.input.add_to_history(value)
            bridge.submit_input(value)

        def action_cancel_or_exit(self) -> None:
            bridge.cancel_or_exit()

        def action_cycle_theme(self) -> None:
            gate = getattr(quimera_app, "input_gate", None)
            handler = getattr(gate, "_theme_cycle_handler", None)
            if callable(handler):
                handler()

        def on_input_changed(self, event: Input.Changed) -> None:
            if not isinstance(event.input, _CompletionInput):
                return
            dropdown = self.query_one(CompletionDropdown)
            value = str(event.value)

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
                return
            if event.kind == "summarizing":
                if event.payload:
                    self._start_spinner()
                else:
                    self._stop_spinner()
                return
            if event.kind == "prompt":
                payload = event.payload or {}
                toolbar = str(payload.get("toolbar", ""))
                self._commands = list(payload.get("commands", []) or [])
                self.query_one("#toolbar", Static).update(toolbar)
                input_widget = self.query_one("#input", Input)
                prompt = str(payload.get("prompt") or "mensagem...").strip()
                input_widget.placeholder = prompt or "mensagem..."
                input_widget.focus()
                return
            if not self._feed_model.apply(event):
                return
            feed = self.query_one("#feed", RichLog)
            feed.clear()
            for item in self._feed_model.items:
                renderable = _render_event(item.event)
                if renderable is not None:
                    feed.write(renderable)

    bridge.attach_quimera_app(quimera_app)
    renderer = getattr(quimera_app, "renderer", None)
    if renderer is not None and hasattr(renderer, "set_profile_resolver"):
        renderer.set_profile_resolver(quimera_app._resolve_profile_style)
    QuimeraTextualApp().run()

    # Após o Textual sair (tela alternativa restaurada), drena eventos pendentes
    # e imprime erros/warnings para a tela normal — assim não desaparecem.
    _screen_handler.drain_to_stderr()
    for _ev in bridge.drain_pending_events():
        _append_post_exit_failure_message(_post_exit_messages, _ev)
    for _kind, _content in _post_exit_messages:
        if _content:
            print(_content, file=sys.stderr, flush=True)
