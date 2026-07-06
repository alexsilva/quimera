"""Bridge thread-safe entre a aplicação e a UI Textual."""
from __future__ import annotations

import queue
import threading

import quimera.themes as themes
from quimera.constants import CMD_EXIT
from quimera.ui.textual.direct_input import DirectInputState
from quimera.ui.textual.events import TextualUiEvent


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
        self._active_agent_styles: dict[str, str] = {}
        self._direct_input_depth = 0
        self._textual_thread_id: int | None = None
        self._lock = threading.Lock()

    def attach_textual_app(self, textual_app) -> None:
        """Registra a instância Textual ativa."""
        with self._lock:
            self.textual_app = textual_app
            self._textual_thread_id = threading.get_ident()

    def attach_quimera_app(self, quimera_app) -> None:
        """Registra a instância Quimera controlada pela UI."""
        with self._lock:
            self.quimera_app = quimera_app

    def create_renderer(self):
        """Cria renderer compatível com o contrato usado pelo Quimera."""
        from quimera.ui.textual.renderer import TextualRenderer

        return TextualRenderer(self)

    def create_input_gate(self, **kwargs):
        """Cria input gate compatível com o contrato usado pelo Quimera."""
        from quimera.ui.textual.input_gate import TextualInputGate

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
            self._emit_user_message(text)
            return
        self._emit_user_message(text)
        self.input_queue.put(value)

    def _emit_user_message(self, text: str) -> None:
        """Espelha mensagens humanas no feed antes de despachar para o agente."""
        clean = str(text).strip()
        if not clean or clean.startswith("/"):
            return
        label = "Alex"
        with self._lock:
            user_name = getattr(self.quimera_app, "user_name", None)
        if str(user_name or "").strip():
            label = str(user_name).strip()
        self.emit(
            TextualUiEvent(
                "user_message",
                {"content": clean, "label": label, "style": "green", "theme": themes.DEFAULT_THEME},
            )
        )

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

    def set_agent_active(self, agent: str, label: str, style: str = "cyan") -> None:
        """Marca agente como ativo para estado da toolbar."""
        key = str(agent or "")
        if not key:
            return
        with self._lock:
            self._active_agent_labels[key] = str(label or key)
            self._active_agent_styles[key] = str(style or "cyan")

    def clear_agent_active(self, agent: str) -> None:
        """Remove agente ativo da toolbar."""
        key = str(agent or "")
        with self._lock:
            self._active_agent_labels.pop(key, None)
            self._active_agent_styles.pop(key, None)

    def active_agent_label(self) -> str | None:
        """Retorna o agente ativo mais recente para exibição na toolbar."""
        with self._lock:
            if not self._active_agent_labels:
                return None
            return next(reversed(self._active_agent_labels.values()))

    def active_agent_info(self) -> tuple[str, str] | None:
        """Retorna (label, style) do agente ativo mais recente."""
        with self._lock:
            if not self._active_agent_labels:
                return None
            latest_key = next(reversed(self._active_agent_labels))
            label = self._active_agent_labels[latest_key]
            style = self._active_agent_styles.get(latest_key, "cyan")
            return label, style

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
            textual_thread_id = self._textual_thread_id
        if textual_app is None:
            self.ui_queue.put(event)
            return
        if threading.get_ident() == textual_thread_id:
            textual_app.handle_bridge_event(event)
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

