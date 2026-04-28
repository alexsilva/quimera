"""Serviços de sessão, persistência e sumarização."""
import threading
import time

from ..constants import MSG_MEMORY_FAILED, MSG_MEMORY_SAVING

# Debounce para save_history: evita serializar JSON inteiro a cada mensagem.
_SAVE_DEBOUNCE_SECONDS = 5.0
_SAVE_DEBOUNCE_MESSAGES = 5


class AppSessionServices:
    """Agrupa persistência do histórico e fechamento de sessão."""

    def __init__(self, app):
        self.app = app
        self._last_save_time: float = 0.0
        self._unsaved_messages: int = 0

    def persist_message(self, role, content):
        """Persiste mensagem no histórico, log e snapshot."""
        app = self.app
        with app._lock:
            app.history.append({"role": role, "content": content})
            app.storage.append_log(role, content)
            self._unsaved_messages += 1
            now = time.monotonic()
            if self._unsaved_messages >= _SAVE_DEBOUNCE_MESSAGES or (now - self._last_save_time) >= _SAVE_DEBOUNCE_SECONDS:
                app.storage.save_history(app.history, shared_state=app.shared_state)
                self._last_save_time = now
                self._unsaved_messages = 0
            session_metrics = getattr(app, "session_metrics", None)
            if session_metrics is not None:
                session_metrics.update_persisted_message_metrics(app, role, content)

    def maybe_auto_summarize(self, preferred_agent=None):
        """Sumariza e trunca o histórico quando excede o threshold configurado."""
        app = self.app
        threshold = getattr(app, "auto_summarize_threshold", None)
        if not isinstance(threshold, int) or threshold <= 0:
            return
        if len(app.history) < threshold:
            return

        keep = app.prompt_builder.history_window
        to_summarize = app.history[:-keep]
        recent = app.history[-keep:]
        existing_summary = app.context_manager.load_session_summary()

        app.renderer.show_system(
            f"[memória] histórico com {len(app.history)} mensagens — gerando resumo automático..."
        )
        summary_agent_preference = preferred_agent or getattr(
            app,
            "summary_agent_preference",
            app.active_agents[0],
        )
        summary = app.session_summarizer.summarize(
            to_summarize,
            existing_summary=existing_summary,
            preferred_agent=summary_agent_preference,
        )
        if summary:
            app.context_manager.update_with_summary(summary)
            app.history = recent
            app.storage.save_history(app.history, shared_state=app.shared_state)
            app.renderer.show_system(
                f"[memória] histórico truncado para {len(app.history)} mensagens recentes"
            )
        else:
            app.renderer.show_system("[memória] resumo automático falhou — histórico mantido")

    def shutdown(self):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        app = self.app
        app.task_services.stop_task_executors()
        runtime_readline = app.readline if hasattr(app, "readline") else None
        if runtime_readline:
            try:
                runtime_readline.write_history_file(str(app.history_file))
            except Exception:
                pass

        if not app.history:
            return

        app.renderer.show_system(MSG_MEMORY_SAVING)

        result = [None]

        def _run_summary():
            try:
                result[0] = app.session_summarizer.summarize(
                    app.history,
                    existing_summary=app.context_manager.load_session_summary(),
                    preferred_agent=getattr(app, "summary_agent_preference", None),
                )
            except Exception:
                pass

        worker = threading.Thread(target=_run_summary, daemon=True)
        worker.start()
        try:
            worker.join(timeout=30)
        except KeyboardInterrupt:
            if app.agent_client:
                app.agent_client._user_cancelled = True
                app.agent_client._cancel_event.set()
            app.renderer.show_system(MSG_MEMORY_FAILED.strip())
            try:
                worker.join(timeout=1)
            except KeyboardInterrupt:
                pass
            return
        summary = result[0]
        if summary:
            app.context_manager.update_with_summary(summary)
        else:
            app.renderer.show_system(MSG_MEMORY_FAILED)
