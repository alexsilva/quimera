"""Lifecycle explícito da aplicação Quimera."""

from __future__ import annotations

import os
from typing import Any

from ..runtime.tools.todo import TodoRegistry


class AppLifecycle:
    """Coordena start/close idempotentes dos recursos de runtime."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._started = False
        self._closed = False

    def start(self) -> None:
        """Inicializa recursos que dependem do wiring completo."""
        if self._started:
            return
        self._started = True
        self._closed = False
        self._app._setup_task_executors()

    def close(self, *, interrupted: bool = False) -> None:
        """Fecha recursos uma única vez, na ordem inversa da inicialização."""
        if self._closed:
            return
        self._closed = True
        app = self._app
        try:
            app._stop_task_executors()
        except Exception:
            pass
        process_supervisor = getattr(app, "process_supervisor", None)
        if process_supervisor is not None:
            process_supervisor.shutdown()
        try:
            session_services = getattr(app, "session_services", None)
            if session_services is not None:
                session_services.shutdown(interrupted=interrupted)
        finally:
            current_job_id = getattr(app, "current_job_id", None)
            if current_job_id is not None:
                TodoRegistry.cleanup(current_job_id)
            agent_client = getattr(app, "agent_client", None)
            if agent_client is not None:
                agent_client.close()
            renderer = getattr(app, "renderer", None)
            if renderer is not None and hasattr(renderer, "close"):
                renderer.close()
            run_render_bug_detector = getattr(app, "_run_render_bug_detector", None)
            if callable(run_render_bug_detector):
                run_render_bug_detector()
            behavior_metrics = getattr(app, "behavior_metrics", None)
            if behavior_metrics is not None:
                behavior_metrics._flush_if_dirty()
            app._restore_current_job_env()
            bug_store = getattr(app, "bug_store", None)
            if bug_store is not None and hasattr(bug_store, "close"):
                try:
                    bug_store.close()
                except Exception:
                    pass

    def restore_current_job_env(self) -> None:
        """Restaura QUIMERA_CURRENT_JOB_ID ao valor anterior ao app."""
        previous = self._app._previous_current_job_id_env
        if previous is None:
            os.environ.pop("QUIMERA_CURRENT_JOB_ID", None)
        else:
            os.environ["QUIMERA_CURRENT_JOB_ID"] = previous

    def __enter__(self) -> "AppLifecycle":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close(interrupted=exc_type is KeyboardInterrupt)
        return False
