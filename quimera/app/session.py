"""Serviços de sessão, persistência e sumarização."""
from __future__ import annotations

import sys
import threading
import time

from ..constants import MSG_MEMORY_FAILED, MSG_MEMORY_SAVING
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
        history: list,
        storage: ISessionStorage,
        renderer: IRenderer,
        agent_pool: IAgentPool,
        lock: threading.Lock,
        context_manager,
        session_summarizer,
        task_services,
        prompt_builder,
        shared_state: dict,
        auto_summarize_threshold: int | None = None,
        summary_agent_preference: str | None = None,
        agent_client=None,
    ):
        self._history = history
        self._storage = storage
        self._renderer = renderer
        self._agent_pool = agent_pool
        self._lock = lock
        self._context_manager = context_manager
        self._session_summarizer = session_summarizer
        self._task_services = task_services
        self._prompt_builder = prompt_builder
        self._shared_state = shared_state
        self._auto_summarize_threshold = auto_summarize_threshold
        self._summary_agent_preference = summary_agent_preference
        self._agent_client = agent_client
        self._last_save_time: float = 0.0
        self._unsaved_messages: int = 0

    def _enforce_history_limit(self) -> int:
        """Aplica o teto do histórico mesmo quando o resumo automático não roda."""
        prompt_builder = self._prompt_builder
        limit = compute_history_hard_limit(
            getattr(prompt_builder, "history_window", None) if prompt_builder else None,
            self._auto_summarize_threshold,
        )
        trimmed_history, dropped = trim_history_messages(self._history, limit)
        if dropped:
            self._history[:] = trimmed_history
        return dropped

    def persist_message(self, role, content, *, return_history_snapshot: bool = False):
        """Persiste mensagem no histórico, log e snapshot."""
        with self._lock:
            self._history.append({"role": role, "content": content})
            self._storage.append_log(role, content)
            self._enforce_history_limit()
            self._unsaved_messages += 1
            now = time.monotonic()
            if self._unsaved_messages >= _SAVE_DEBOUNCE_MESSAGES or (now - self._last_save_time) >= _SAVE_DEBOUNCE_SECONDS:
                self._storage.save_history(self._history, shared_state=self._shared_state)
                self._last_save_time = now
                self._unsaved_messages = 0
            if return_history_snapshot:
                return list(self._history)
        return None

    def maybe_auto_summarize(self, preferred_agent=None):
        """Sumariza e trunca o histórico quando excede o threshold configurado."""
        threshold = self._auto_summarize_threshold
        if not isinstance(threshold, int) or threshold <= 0:
            return
        if len(self._history) < threshold:
            return

        prompt_builder = self._prompt_builder
        keep = prompt_builder.history_window if prompt_builder else 0
        if len(self._history) <= keep:
            return
        if len(self._history) - keep < _MIN_SUMMARIZE_SURPLUS:
            return
        to_summarize = self._history[:-keep]
        recent = self._history[-keep:]
        existing_summary = self._context_manager.load_session_summary()

        self._renderer.show_system(
            f"[memória] histórico com {len(self._history)} mensagens — gerando resumo automático..."
        )
        summary_agent = preferred_agent or self._summary_agent_preference or self._agent_pool.primary
        summary = self._session_summarizer.summarize(
            to_summarize,
            existing_summary=existing_summary,
            preferred_agent=summary_agent,
        )
        if summary:
            self._context_manager.update_with_summary(summary)
            self._history[:] = recent
            self._storage.save_history(self._history, shared_state=self._shared_state)
            self._renderer.show_system(
                f"[memória] histórico truncado para {len(self._history)} mensagens recentes"
            )
        else:
            self._renderer.show_system("[memória] resumo automático falhou — histórico mantido")

    def shutdown(self):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        self._task_services.stop_task_executors()

        if not self._history:
            return

        self._renderer.show_system(MSG_MEMORY_SAVING)

        result = [None]

        def _run_summary():
            try:
                result[0] = self._session_summarizer.summarize(
                    self._history,
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
                self._agent_client._cancel_event.set()
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
