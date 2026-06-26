"""Interface Textual principal do Quimera."""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

_NO_RESPONSE_MESSAGE = "sem resposta válida"


@dataclass
class TextualUiEvent:
    """Evento thread-safe enviado do runtime para a UI Textual."""

    kind: str
    payload: Any = None
    agent: str | None = None


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
        """Compatibilidade: Textual redesenha via event loop."""
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

    def read_plain_input(self, prompt: str) -> str:
        """Lê uma linha simples pelo mesmo input Textual."""
        return self(prompt)

    def read_selection_in_terminal(
        self,
        question: str,
        options: list[str],
        timeout: float = 300.0,
        owner: str | None = None,
    ) -> tuple[int, str] | None:
        """Lê seleção por número/texto no input Textual."""
        self._bridge.emit(
            TextualUiEvent(
                "question",
                {"question": question, "options": list(options), "owner": owner},
            )
        )
        try:
            raw = self._bridge.input_queue.get(timeout=timeout).strip()
        except queue.Empty:
            return None
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
        self._bridge.emit(
            TextualUiEvent(
                "question",
                {"question": f"{question}\n{prompt}", "options": [], "owner": owner},
            )
        )
        try:
            return self._bridge.input_queue.get(timeout=timeout)
        except queue.Empty:
            return None


class TextualRenderer:
    """Renderer compatível com a API usada pelo Quimera, emitindo para Textual."""

    supports_agent_feed = True

    def __init__(self, bridge: TextualUiBridge) -> None:
        self._bridge = bridge
        self._audit_logger = None

    def set_prompt_integration(self, is_active_fn, run_above_fn) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

    def close(self, timeout: float = 5.0) -> None:
        """Compatibilidade com TerminalRenderer."""
        return None

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
        self._bridge.emit(
            TextualUiEvent(
                "agent_message",
                {"content": str(content or ""), "render_mode": render_mode},
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
        from textual.containers import Vertical
        from textual.widgets import Header, Input, RichLog, Static
    except ImportError as exc:
        raise SystemExit(
            "A interface Textual requer a dependência 'textual'. "
            "Reinstale com: pip install -e ."
        ) from exc

    class QuimeraTextualApp(App):
        """TUI principal do Quimera."""

        CSS = """
        Screen {
            layout: vertical;
        }
        #feed {
            height: 1fr;
        }
        #toolbar {
            height: 1;
            color: $text-muted;
            background: $surface;
        }
        #input {
            dock: bottom;
        }
        """

        BINDINGS = [
            ("tab", "complete_command", "Completar"),
            ("ctrl+c", "cancel_or_exit", "Cancelar/Sair"),
            ("ctrl+t", "cycle_theme", "Tema"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._worker_thread: threading.Thread | None = None
            self._commands: list[str] = []

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical():
                yield RichLog(id="feed", markup=True, wrap=True, highlight=False)
                yield Static("", id="toolbar")
                yield Input(placeholder="mensagem...", id="input")

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

        def _run_quimera_app(self) -> None:
            try:
                quimera_app.run()
            finally:
                try:
                    self.call_from_thread(self.exit)
                except RuntimeError:
                    return

        def on_input_submitted(self, event: Input.Submitted) -> None:
            event.stop()
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

        def action_complete_command(self) -> None:
            input_widget = self.query_one("#input", Input)
            value = str(input_widget.value or "")
            if not value.startswith("/") or " " in value:
                return
            matches = [command for command in self._commands if command.startswith(value)]
            if len(matches) == 1:
                input_widget.value = f"{matches[0]} "
                input_widget.cursor_position = len(input_widget.value)

        def handle_bridge_event(self, event: TextualUiEvent) -> None:
            if event.kind == "prompt":
                payload = event.payload or {}
                toolbar = str(payload.get("toolbar", ""))
                self._commands = list(payload.get("commands", []) or [])
                self.query_one("#toolbar", Static).update(toolbar)
                input_widget = self.query_one("#input", Input)
                input_widget.placeholder = "mensagem..."
                input_widget.focus()
                return
            renderable = _render_event(event)
            if renderable is None:
                return
            feed = self.query_one("#feed", RichLog)
            feed.write(renderable)

    bridge.attach_quimera_app(quimera_app)
    QuimeraTextualApp().run()
