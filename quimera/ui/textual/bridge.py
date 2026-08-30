"""Bridge thread-safe entre a aplicação e a UI Textual."""
from __future__ import annotations

import queue
import threading
import time

from quimera.app.submission_tracker import SubmittedInput, SubmissionRecord, SubmissionTracker
from quimera.agents.capabilities import is_agent_running
import quimera.themes as themes
from quimera.constants import CMD_EXIT
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
        self._direct_input_owners: dict[int, tuple[threading.Thread, int]] = {}
        self._textual_thread_id: int | None = None
        self._lock = threading.Lock()
        self.submission_tracker = SubmissionTracker(self._emit_submission_status)

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
        if not text.strip():
            # Linha vazia não inicia rodada de chat com agente.
            return
        if self._try_inject_active_agent(text):
            self._emit_user_message(text)
            return
        clean = self._visible_user_message(text)
        submission = None
        queued_value = value
        if self._should_track_submission(text, clean):
            with self._lock:
                watch = self.textual_app is not None
            submission = self.submission_tracker.start(emit=False, watch=watch)
            self._register_submission(submission.submission_id)
            queued_value = SubmittedInput(value, submission.submission_id)
        self._emit_user_message(text, clean=clean, submission=submission)
        self.input_queue.put(queued_value)

    def _emit_user_message(
        self,
        text: str,
        *,
        clean: str | None = None,
        submission: SubmissionRecord | None = None,
    ) -> None:
        """Espelha mensagens humanas no feed antes de despachar para o agente."""
        clean = self._visible_user_message(text) if clean is None else clean
        if not clean:
            return
        label = "Alex"
        with self._lock:
            user_name = getattr(self.quimera_app, "user_name", None)
        if str(user_name or "").strip():
            label = str(user_name).strip()
        payload = {
            "content": clean,
            "label": label,
            "style": "green",
            "theme": themes.DEFAULT_THEME,
        }
        if submission is not None:
            payload["submission_id"] = submission.submission_id
            payload["submission"] = self._stamp_submission_payload(submission.as_payload())
        self.emit(TextualUiEvent("user_message", payload))

    @staticmethod
    def _stamp_submission_payload(payload: dict[str, object]) -> dict[str, object]:
        """Ancora o payload no relógio local para o tempo correr ao vivo na UI."""
        payload["received_monotonic"] = time.monotonic()
        return payload

    @staticmethod
    def _should_track_submission(text: str, visible_text: str) -> bool:
        """Rastreia chats normais e prompts direcionados, não comandos de controle."""
        clean = str(text or "").strip()
        if not visible_text or not clean:
            return False
        if not clean.startswith("/"):
            return True
        return clean.split(maxsplit=1)[0].casefold() != "/debate"

    def _emit_submission_status(self, payload: dict[str, object]) -> None:
        """Publica atualização para substituir o estado no turno já existente."""
        self.emit(TextualUiEvent("submission_status", self._stamp_submission_payload(payload)))

    def update_submission_status(self, submission_id: str, status: str, **metadata):
        """Atualiza uma submissão a partir do scheduler/lifecycle."""
        return self.submission_tracker.transition(submission_id, status, **metadata)

    def _register_submission(self, submission_id: str) -> None:
        with self._lock:
            lifecycle = getattr(self.quimera_app, "chat_lifecycle", None)
        register = getattr(lifecycle, "register_submission", None)
        if callable(register):
            register(submission_id)

    def _visible_user_message(self, text: str) -> str:
        """Remove prefixo de agente e oculta comandos de controle do feed."""
        clean = str(text).strip()
        if not clean or not clean.startswith("/"):
            return clean

        parts = clean.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            return ""
        requested_prefix = parts[0].casefold()
        if requested_prefix == "/debate":
            try:
                from quimera.debate.commands import parse_debate_command

                parsed = parse_debate_command(clean)
            except Exception:
                return ""
            return parsed.topic if parsed.action == "start" else ""
        with self._lock:
            quimera_app = self.quimera_app
        get_profiles = getattr(quimera_app, "get_active_agent_profiles", None)
        if not callable(get_profiles):
            return ""
        try:
            active_profiles = list(get_profiles() or ())
        except Exception:
            return ""
        for profile in active_profiles:
            prefixes = (
                getattr(profile, "prefix", None),
                *(getattr(profile, "aliases", None) or ()),
            )
            if any(
                requested_prefix == str(prefix or "").strip().casefold()
                for prefix in prefixes
            ):
                return parts[1].strip()
        return ""

    def begin_direct_input(self) -> None:
        """Força submissões seguintes a irem para o prompt inline ativo."""
        with self._lock:
            self._prune_direct_input_owners_locked()
            thread = threading.current_thread()
            owner_id = thread.ident or id(thread)
            _owner, depth = self._direct_input_owners.get(owner_id, (thread, 0))
            self._direct_input_owners[owner_id] = (thread, depth + 1)
            self._sync_direct_input_depth_locked()

    def end_direct_input(self) -> None:
        """Libera roteamento direto quando o prompt inline termina."""
        with self._lock:
            thread = threading.current_thread()
            owner_id = thread.ident or id(thread)
            owned = self._direct_input_owners.get(owner_id)
            if owned is not None:
                if owned[1] <= 1:
                    self._direct_input_owners.pop(owner_id, None)
                else:
                    self._direct_input_owners[owner_id] = (owned[0], owned[1] - 1)
            self._sync_direct_input_depth_locked()

    def is_direct_input_active(self) -> bool:
        """Retorna True se há prompt inline aguardando resposta."""
        with self._lock:
            self._prune_direct_input_owners_locked()
            return self._direct_input_depth > 0

    def _prune_direct_input_owners_locked(self) -> None:
        stale = [
            owner_id
            for owner_id, (thread, _depth) in self._direct_input_owners.items()
            if not thread.is_alive()
        ]
        for owner_id in stale:
            self._direct_input_owners.pop(owner_id, None)
        self._sync_direct_input_depth_locked()

    def _sync_direct_input_depth_locked(self) -> None:
        self._direct_input_depth = sum(
            depth for _thread, depth in self._direct_input_owners.values()
        )

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

    @staticmethod
    def _has_active_chat_work(quimera_app) -> bool:
        """Retorna True quando o scheduler do chat possui trabalho aceito.

        O AgentClient principal não é uma fonte confiável nesse caso: rodadas
        normais podem executar em clients isolados. O estado canônico é o
        contador de prompts ativos/pendentes do runtime.
        """
        runtime_state = getattr(quimera_app, "runtime_state", None)
        get_outstanding = getattr(runtime_state, "get_chat_outstanding_count", None)
        if callable(get_outstanding):
            try:
                if int(get_outstanding() or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        agent_client = getattr(quimera_app, "agent_client", None)
        has_active_work = getattr(agent_client, "has_active_work", None)
        if callable(has_active_work):
            try:
                if bool(has_active_work()):
                    return True
            except Exception:
                pass
        return bool(getattr(quimera_app, "is_agent_running", False))

    def cancel_or_exit(self) -> None:
        """Cancela agente ativo ou solicita saída limpa."""
        with self._lock:
            quimera_app = self.quimera_app
        if self._has_active_chat_work(quimera_app):
            lifecycle = getattr(quimera_app, "chat_lifecycle", None)
            handle_interrupt = getattr(lifecycle, "handle_local_interrupt", None)
            if callable(handle_interrupt):
                handle_interrupt()
                return
        agent_client = getattr(quimera_app, "agent_client", None)
        if is_agent_running(agent_client):
            cancel = getattr(agent_client, "cancel_active_work", None)
            if callable(cancel):
                cancel()
                return
        self.submit_input("/exit")
