"""Serviços de sessão, persistência e sumarização."""
from __future__ import annotations

import sys
import threading
import time

from ..constants import MSG_MEMORY_FAILED, MSG_MEMORY_SAVING
from ..domain.session_state import SessionState
from .interfaces import IAgentPool, IRenderer, ISessionStorage

# Debounce para save_history: evita serializar JSON inteiro a cada mensagem.
_SAVE_DEBOUNCE_SECONDS = 5.0
_SAVE_DEBOUNCE_MESSAGES = 5
_MIN_HISTORY_HARD_LIMIT = 24
_MIN_SUMMARIZE_SURPLUS = 10


def compute_history_hard_limit(history_window, auto_summarize_threshold) -> int:
    """Calcula um teto defensivo para o histórico em memória."""
    limits = [_MIN_HISTORY_HARD_LIMIT]
    if isinstance(history_window, int) and history_window > 0:
        limits.append(history_window * 4)
    if isinstance(auto_summarize_threshold, int) and auto_summarize_threshold > 0:
        limits.append(auto_summarize_threshold * 2)
    return max(limits)


def trim_history_messages(history, limit):
    """Mantém apenas a cauda mais recente do histórico."""
    if not isinstance(limit, int) or limit <= 0 or len(history) <= limit:
        return history, 0
    dropped = len(history) - limit
    return history[-limit:], dropped


class AppSessionServices:
    """Agrupa persistência do histórico e fechamento de sessão."""

    def __init__(
        self,
        session_state: SessionState,
        storage: ISessionStorage,
        renderer: IRenderer,
        agent_pool: IAgentPool,
        context_manager,
        session_summarizer,
        task_services,
        prompt_builder,
        auto_summarize_threshold: int | None = None,
        summary_agent_preference: str | None = None,
        agent_client=None,
    ):
        self._session_state = session_state
        # Compatibilidade: outras camadas ainda observam a mesma lista/dict mutáveis.
        self._history = session_state.history
        self._shared_state = session_state.shared_state
        self._lock = session_state.history_lock
        self._counter_lock = threading.Lock()
        self._storage = storage
        self._renderer = renderer
        self._agent_pool = agent_pool
        self._context_manager = context_manager
        self._session_summarizer = session_summarizer
        self._task_services = task_services
        self._prompt_builder = prompt_builder
        self._auto_summarize_threshold = auto_summarize_threshold
        self._summary_agent_preference = summary_agent_preference
        self._agent_client = agent_client
        self._last_save_time: float = 0.0
        self._unsaved_messages: int = 0

    def _history_hard_limit(self) -> int:
        """Calcula o limite defensivo atual para o histórico em memória."""
        prompt_builder = self._prompt_builder
        return compute_history_hard_limit(
            getattr(prompt_builder, "history_window", None) if prompt_builder else None,
            self._auto_summarize_threshold,
        )

    def _enforce_history_limit(self) -> int:
        """Aplica o teto do histórico mesmo quando o resumo automático não roda."""
        dropped, _ = self._session_state.trim_history(self._history_hard_limit())
        return dropped

    def persist_message(self, role, content, *, return_history_snapshot: bool = False):
        """Persiste mensagem no histórico, log e snapshot."""
        _, history_snapshot = self._session_state.append_history_trimmed_and_snapshot(
            {"role": role, "content": content},
            self._history_hard_limit(),
        )
        self._storage.append_log(role, content)

        with self._counter_lock:
            self._unsaved_messages += 1
            now = time.monotonic()
            should_save = (
                self._unsaved_messages >= _SAVE_DEBOUNCE_MESSAGES
                or (now - self._last_save_time) >= _SAVE_DEBOUNCE_SECONDS
            )
            if should_save:
                self._storage.save_history(
                    history_snapshot,
                    shared_state=self._session_state.shared_state_snapshot(),
                )
                self._last_save_time = now
                self._unsaved_messages = 0
            if return_history_snapshot:
                return history_snapshot
        return None

    def maybe_auto_summarize(self, preferred_agent=None):
        """Sumariza e trunca o histórico quando excede o threshold configurado."""
        threshold = self._auto_summarize_threshold
        if not isinstance(threshold, int) or threshold <= 0:
            return

        prompt_builder = self._prompt_builder
        keep = prompt_builder.history_window if prompt_builder else 0
        history_snapshot = self._session_state.history_snapshot()
        history_len = len(history_snapshot)
        if history_len < threshold:
            return
        if history_len <= keep:
            return
        if history_len - keep < _MIN_SUMMARIZE_SURPLUS:
            return
        if keep > 0:
            to_summarize = history_snapshot[:-keep]
            recent = history_snapshot[-keep:]
        else:
            to_summarize = history_snapshot
            recent = []

        existing_summary = self._context_manager.load_session_summary()

        self._renderer.show_system(
            f"[memória] histórico com {history_len} mensagens — gerando resumo automático..."
        )
        summary_agent = preferred_agent or self._summary_agent_preference or self._agent_pool.primary
        summary = self._session_summarizer.summarize(
            to_summarize,
            existing_summary=existing_summary,
            preferred_agent=summary_agent,
        )
        if summary:
            matched, final_history = self._session_state.replace_history_if_prefix_matches(
                history_snapshot,
                history_len,
                recent,
            )
            if not matched:
                self._renderer.show_system(
                    "[memória] histórico mudou durante o resumo — truncamento adiado"
                )
                return
            self._storage.save_history(
                final_history,
                shared_state=self._session_state.shared_state_snapshot(),
            )
            current_len = len(final_history)
            self._context_manager.update_with_summary(summary)
            self._renderer.show_system(
                f"[memória] histórico truncado para {current_len} mensagens recentes"
            )
        else:
            self._renderer.show_system("[memória] resumo automático falhou — histórico mantido")

    def _flush_pending_history(self) -> None:
        """Garante persistência do histórico em memória antes do encerramento."""
        with self._counter_lock:
            if self._unsaved_messages <= 0:
                return
            history_snapshot = self._session_state.history_snapshot()
            shared_state_snapshot = self._session_state.shared_state_snapshot()
            self._storage.save_history(
                history_snapshot,
                shared_state=shared_state_snapshot,
            )
            self._last_save_time = time.monotonic()
            self._unsaved_messages = 0

    def shutdown(self, *, interrupted: bool = False):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        self._task_services.stop_task_executors()
        self._flush_pending_history()

        history_snapshot = self._session_state.history_snapshot()

        if not history_snapshot or interrupted:
            return

        self._renderer.show_system(MSG_MEMORY_SAVING)

        result = [None]

        def _run_summary():
            try:
                result[0] = self._session_summarizer.summarize(
                    history_snapshot,
                    existing_summary=self._context_manager.load_session_summary(),
                    preferred_agent=self._summary_agent_preference,
                )
            except Exception:
                pass

        worker = threading.Thread(target=_run_summary, daemon=True)
        worker.start()
        try:
            worker.join(timeout=90)
        except KeyboardInterrupt:
            if self._agent_client:
                self._agent_client._user_cancelled = True
                cancel_event = getattr(self._agent_client, "_cancel_event", None)
                if cancel_event is not None and hasattr(cancel_event, "set"):
                    cancel_event.set()
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
            self._renderer.show_system(MSG_MEMORY_FAILED)
            try:
                worker.join(timeout=1)
            except KeyboardInterrupt:
                pass
            return
        summary = result[0]
        if summary:
            self._context_manager.update_with_summary(summary)
        else:
            self._renderer.show_system(MSG_MEMORY_FAILED)
