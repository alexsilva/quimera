"""Interface Textual principal do Quimera."""
from __future__ import annotations

import logging
import queue
import sys
import traceback
import threading
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from quimera.ui.text import _extract_text_from_renderable, strip_ansi
from quimera.app.config import handler as _screen_handler

_NO_RESPONSE_MESSAGE = "sem resposta válida"


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


class _FeedEntryBuffer:
    """Mantém apenas as últimas entradas visíveis do feed."""

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit if isinstance(limit, int) and limit > 0 else None
        self._entries: deque[Any] = deque()

    def append(self, renderable: Any) -> list[Any]:
        """Adiciona uma entrada e devolve snapshot já podado."""
        self._entries.append(renderable)
        if self._limit is not None:
            while len(self._entries) > self._limit:
                self._entries.popleft()
        return list(self._entries)


def _resolve_textual_feed_limit(quimera_app) -> int | None:
    """Resolve o limite visual do feed a partir da config já carregada no app."""
    auto_summarize_threshold = getattr(quimera_app, "auto_summarize_threshold", None)
    if isinstance(auto_summarize_threshold, int) and auto_summarize_threshold > 0:
        return auto_summarize_threshold
    prompt_builder = getattr(quimera_app, "prompt_builder", None)
    history_window = getattr(prompt_builder, "history_window", None) if prompt_builder else None
    if isinstance(history_window, int) and history_window > 0:
        return history_window
    return None


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
            return self._active

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

    def set_prompt_integration(self, is_active_fn, run_above_fn) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def close(self, timeout: float = 5.0) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def external_window(self, window_id: str, title: str = "", metadata=None):
        """Compatibilidade: Textual mantém a UI como dona do terminal."""
        self._bridge.emit(TextualUiEvent("system", title or window_id))
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

    def show_plain(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto simples."""
        kind = "muted" if muted else "plain"
        self._bridge.emit(TextualUiEvent(kind, str(message), agent=agent))

    def show_feed(self, message: str, agent=None, muted: bool = False) -> None:
        """Exibe texto no feed."""
        self.show_plain(message, agent=agent, muted=muted)

    def show_message(self, agent, content, render_mode: str = "auto") -> None:
        """Exibe resposta final de agente."""
        clean_content = strip_ansi(_extract_text_from_renderable(content))
        self._bridge.emit(
            TextualUiEvent(
                "agent_message",
                {"content": clean_content, "render_mode": render_mode},
                agent=str(agent),
            )
        )

    def show_no_response(self, agent) -> None:
        """Exibe ausência de resposta."""
        self.show_message(agent, _NO_RESPONSE_MESSAGE, render_mode="plain")

    def start_message_stream(self, agent) -> None:
        """Inicia stream visual."""
        self._bridge.emit(TextualUiEvent("stream_start", "", agent=str(agent)))

    def update_message_stream(self, agent, chunk) -> None:
        """Atualiza stream visual."""
        self._bridge.emit(TextualUiEvent("stream_chunk", chunk, agent=str(agent)))

    def finish_message_stream(
        self,
        agent,
        final_content: str,
        render_mode: str = "auto",
    ) -> None:
        """Finaliza stream visual."""
        self.show_message(agent, final_content, render_mode=render_mode)

    def commit_agent_stream(self, agent, render_mode: str = "auto") -> bool:
        """Compatibilidade com TerminalRenderer."""
        return False

    def abort_message_stream(self, agent) -> None:
        """Aborta stream visual."""
        self._bridge.emit(TextualUiEvent("stream_abort", "", agent=str(agent)))

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
        agent = event.agent or "agente"
        body = Markdown(content) if payload.get("render_mode") != "plain" else Text(content)
        return Panel(body, title=f"🤖 {agent}", border_style="cyan")
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

        def key_down(self) -> None:
            dropdown = self.app.query_one(CompletionDropdown)
            if dropdown.has_options:
                dropdown.select_next()

        def key_tab(self) -> None:
            dropdown = self.app.query_one(CompletionDropdown)
            selected = dropdown.get_selected()
            if selected:
                self.value = f"{selected} "
                self.cursor_position = len(self.value)
                dropdown.hide()
                return

    _SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

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
        """

        TITLE = "Quimera"

        BINDINGS = [
            ("ctrl+c", "cancel_or_exit", "Cancelar/Sair"),
            ("ctrl+t", "cycle_theme", "Tema"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._worker_thread: threading.Thread | None = None
            self._commands: list[str] = []
            self._summarizing = False
            self._spinner_index = 0
            self._spinner_timer = None
            self._feed_buffer = _FeedEntryBuffer(_resolve_textual_feed_limit(quimera_app))

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical(id="main"):
                yield RichLog(id="feed", markup=True, wrap=True, highlight=False)
                yield Static("", id="toolbar")
                yield CompletionDropdown()
                with Vertical(id="input_bar"):
                    yield _CompletionInput(placeholder="mensagem...", id="input")

        def on_mount(self) -> None:
            bridge.attach_textual_app(self)
            for event in bridge.drain_pending_events():
                self.handle_bridge_event(event)
            self._worker_thread = threading.Thread(
                target=self._run_quimera_app,
                daemon=True,
                name="quimera-textual-loop",
            )
            self._worker_thread.start()
            self.query_one("#input", Input).focus()

        def _start_spinner(self) -> None:
            """Inicia animação de loading no sub_title do header."""
            if self._spinner_timer is not None:
                return
            self._summarizing = True
            self._spinner_index = 0
            self._update_spinner()
            self._spinner_timer = self.set_interval(0.1, self._update_spinner)

        def _stop_spinner(self) -> None:
            """Para animação de loading e limpa o sub_title."""
            self._summarizing = False
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self.sub_title = ""

        def _update_spinner(self) -> None:
            """Avança o frame do spinner no sub_title."""
            frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
            self.sub_title = f"{frame} resumindo..."
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

            if not value or " " in value:
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
            renderable = _render_event(event)
            if renderable is None:
                return
            feed = self.query_one("#feed", RichLog)
            entries = self._feed_buffer.append(renderable)
            feed.clear()
            for entry in entries:
                feed.write(entry)

    bridge.attach_quimera_app(quimera_app)
    QuimeraTextualApp().run()

    # Após o Textual sair (tela alternativa restaurada), drena eventos pendentes
    # e imprime erros/warnings para a tela normal — assim não desaparecem.
    _screen_handler.drain_to_stderr()
    for _ev in bridge.drain_pending_events():
        if _ev.kind in {"error", "warning"}:
            _post_exit_messages.append((_ev.kind, str(_ev.payload or "")))
    for _kind, _content in _post_exit_messages:
        if _content:
            print(_content, file=sys.stderr, flush=True)
