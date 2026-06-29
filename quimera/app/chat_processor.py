"""Processamento do loop de chat interativo do QuimeraApp."""

from __future__ import annotations

import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import logger
from .welcome_presenter import WelcomePresenter
from ..runtime.tools.todo import TodoRegistry
from .tty_control import TtyController
from .session_bootstrap import (
    resolve_render_debug_log_path,
    resolve_session_log_path,
)
from .turn import TurnManager
from .worker import ChatWorker
from ..constants import (
    CMD_EDIT,
    CMD_EXIT,
    CMD_FILE_PREFIX,
    MSG_CHAT_STARTED,
    MSG_SESSION_STATUS,
    MSG_SHUTDOWN,
)


_tty = TtyController()


class _WakeupQueue(queue.Queue):
    """Queue que notifica um Event a cada put(), permitindo espera bloqueante no consumidor."""

    def __init__(self, wakeup_event: threading.Event):
        super().__init__()
        self._wakeup_event = wakeup_event

    def put(self, item, block=True, timeout=None):
        super().put(item, block=block, timeout=timeout)
        self._wakeup_event.set()

    def put_nowait(self, item):
        super().put_nowait(item)
        self._wakeup_event.set()


def run_chat_loop(
    app,
    *,
    chat_worker_cls=ChatWorker,
    turn_manager_cls=TurnManager,
    executor_cls=ThreadPoolExecutor,
) -> None:
    """Executa o ciclo de input/processamento/shutdown do chat."""
    session_state_manager = getattr(app, "session_state_mgr", None)
    if session_state_manager is None:
        raise RuntimeError("QuimeraApp.session_state_mgr não foi inicializado")
    if not hasattr(app, "renderer") or app.renderer is None:
        raise RuntimeError("QuimeraApp.renderer não foi inicializado")
    if not hasattr(app, "session_services") or app.session_services is None:
        raise RuntimeError("QuimeraApp.session_services não foi inicializado")
    chat_lifecycle = getattr(app, "chat_lifecycle", None)
    if chat_lifecycle is None:
        raise RuntimeError("QuimeraApp.chat_lifecycle não foi inicializado")
    _tty.suppress_control_echo()
    show_banner = getattr(app.renderer, "show_banner", app.renderer.show_system)
    show_banner(WelcomePresenter.build_welcome_message())
    workspace = getattr(app, "workspace", None)
    project_path = str(getattr(workspace, "cwd", Path.cwd()))
    _show_neutral = getattr(app.renderer, "show_system_neutral", app.renderer.show_system)
    _show_neutral(f"Projeto: {project_path}")
    _show_neutral = getattr(app.renderer, "show_system_neutral", app.renderer.show_system)
    restore_notice = getattr(app.storage, "pop_restore_notice", lambda: None)()
    if restore_notice:
        _show_neutral(restore_notice)
    _show_neutral(MSG_CHAT_STARTED)
    _show_neutral(
        MSG_SESSION_STATUS.format(
            session_id=app.session_state["session_id"],
            summary_loaded=app._format_yes_no(app.session_state["summary_loaded"]),
        )
    )
    mcp_http_url = getattr(app, "mcp_http_url", None)
    mcp_socket_path = getattr(app, "mcp_socket_path", None)
    if mcp_socket_path:
        _show_neutral(f"MCP interno iniciado em {mcp_socket_path}")
    if mcp_http_url:
        _show_neutral(f"MCP HTTP externo iniciado em {mcp_http_url}")
    if getattr(app, "debug_prompt_metrics", False):
        session_log_path = resolve_session_log_path(app.storage, app.workspace)
        if session_log_path:
            _show_neutral(app._format_session_log_message(session_log_path))
        render_debug_log_path = resolve_render_debug_log_path(
            app.storage, app.workspace, app.debug_prompt_metrics
        )
        if render_debug_log_path:
            _show_neutral(f"Audit de render:\n  {render_debug_log_path}\n")
    flush = getattr(app.renderer, "flush", None)
    if callable(flush):
        flush()

    _ui_wakeup = threading.Event()
    _ui_event_queue: queue.Queue = _WakeupQueue(_ui_wakeup)
    app._ui_event_queue = _ui_event_queue
    chat_lifecycle.bind_ui_event_queue(_ui_event_queue)
    if hasattr(app, "dispatch_services") and app.dispatch_services is not None:
        app.dispatch_services._ui_queue = _ui_event_queue
    if hasattr(app, "event_sink") and app.event_sink is not None:
        app.event_sink._ui_queue = _ui_event_queue
    if not hasattr(app, "turn_manager") or app.turn_manager is None:
        app.turn_manager = turn_manager_cls()
    threaded_chat = app.threads > 1
    if hasattr(app, "input_services") and app.input_services is not None:
        app.input_services.set_nonblocking_tty(threaded_chat)
        if threaded_chat:
            app.input_services.set_wakeup_event(_ui_wakeup)
    chat_queue = None
    chat_worker = None
    chat_executor = None
    chat_slot_semaphore = None
    chat_worker_failure_reported = False
    interrupted_shutdown = False
    swallow_threaded_input_interrupt = False
    ctrl_c_cancelled = False
    if threaded_chat:
        async_capacity = max(1, int(getattr(app, "threads", 1) or 1))
        chat_executor = executor_cls(
            max_workers=async_capacity,
            thread_name_prefix="quimera-chat-prompt",
        )
        chat_slot_semaphore = threading.Semaphore(async_capacity)
        app.runtime_state.chat_executor = chat_executor
        app.runtime_state.chat_slot_semaphore = chat_slot_semaphore
        chat_queue = queue.Queue()
        chat_worker = chat_worker_cls(
            chat_queue=chat_queue,
            ui_event_queue=_ui_event_queue,
            agent_executor=chat_lifecycle.submit_async_message,
            turn_manager=getattr(app, 'turn_manager', None),
        )
        chat_worker.start()
        app.runtime_state.chat_queue = chat_queue

    _pending_async_slot = False
    try:
        while True:
            chat_lifecycle.drain_ui_events(_ui_event_queue)
            if hasattr(app, "event_sink") and app.event_sink is not None:
                app.event_sink.drain_pending()
            if threaded_chat and chat_worker is not None and not chat_worker.is_alive():
                if not chat_worker_failure_reported:
                    logger.error("chat worker morreu; alternando para processamento síncrono")
                    app.system_layer.show_error_message("[erro] worker do chat interrompido; alternando para processamento síncrono.")
                    chat_worker_failure_reported = True
                chat_worker = None
                chat_queue = None
                threaded_chat = False
                app.runtime_state.chat_inflight_count = 0
                app.runtime_state.chat_queue = None
                if chat_executor is not None:
                    chat_executor.shutdown(wait=False, cancel_futures=True)
                    chat_executor = None
                    app.runtime_state.chat_executor = None
                app.runtime_state.chat_slot_semaphore = None
                app._refresh_parallel_toolbar()
                if hasattr(app, "turn_manager"):
                    app.turn_manager.reset()
            if (
                hasattr(app, "turn_manager")
                and not app.turn_manager.is_human_turn
            ):
                if not threaded_chat:
                    if not getattr(app, "_turn_blocked_warning_shown", False):
                        app.renderer.show_system("[Aguardando resposta do agente...]")
                        app._turn_blocked_warning_shown = True
                    app.turn_manager.wait_for_human_turn(timeout=0.01)
                    continue
            app._turn_blocked_warning_shown = False

            try:
                user = app.read_user_input(app._format_user_prompt(), timeout=0)
                if user is not None:
                    swallow_threaded_input_interrupt = False
                    ctrl_c_cancelled = False
            except KeyboardInterrupt:
                if threaded_chat and swallow_threaded_input_interrupt:
                    swallow_threaded_input_interrupt = False
                    continue
                if threaded_chat:
                    inflight = app.runtime_state.get_chat_inflight_count()
                    if inflight > 0 and not ctrl_c_cancelled:
                        ctrl_c_cancelled = True
                        chat_lifecycle.handle_local_interrupt()
                        swallow_threaded_input_interrupt = True
                        continue
                raise
            if user is None:
                if not sys.stdin.isatty():
                    break
                continue

            if user == CMD_EXIT:
                break

            if user.strip() == CMD_EDIT:
                if getattr(app.input_services, "_split_queue", None) is not None:
                    app.system_layer.show_error_message(
                        "[erro] /edit não disponível no modo split"
                    )
                    continue
                content = app.input_services.read_from_editor()
                if not content:
                    continue
                user = content

            elif user.strip().startswith(CMD_FILE_PREFIX):
                path_str = user.strip()[len(CMD_FILE_PREFIX):].strip()
                content = app.input_services.read_from_file(path_str)
                if not content:
                    continue
                user = content

            _cmd_result = app.handle_command(user)
            if _cmd_result is True:
                continue
            elif isinstance(_cmd_result, str):
                user = _cmd_result

            session_state_manager.advance_turn()

            if chat_queue is not None:
                acquired_async_slot = False
                if chat_slot_semaphore is not None:
                    acquired_async_slot = chat_slot_semaphore.acquire(blocking=False)
                if acquired_async_slot:
                    app.runtime_state.increment_chat_inflight(app._refresh_parallel_toolbar)
                    _pending_async_slot = True
                    chat_queue.put(user)
                    _pending_async_slot = False
                    app._refresh_parallel_toolbar()
                    time.sleep(0.001)
                    if (
                        hasattr(app, "turn_manager")
                        and app.turn_manager.is_human_turn
                    ):
                        app.turn_manager.next_turn()
                else:
                    if hasattr(app, "turn_manager") and app.turn_manager.is_human_turn:
                        app.turn_manager.next_turn()
                    try:
                        chat_lifecycle.process_sync_message_with_slot(user)
                    except KeyboardInterrupt:
                        swallow_threaded_input_interrupt = True
                        chat_lifecycle.handle_local_interrupt()
                        continue
                    if hasattr(app, "turn_manager") and app.turn_manager.is_ai_turn:
                        app.turn_manager.next_turn()
            else:
                if hasattr(app, "turn_manager"):
                    app.turn_manager.next_turn()
                try:
                    chat_lifecycle.process_message(user)
                except KeyboardInterrupt:
                    chat_lifecycle.handle_local_interrupt()
                    continue
                if hasattr(app, "turn_manager") and app.turn_manager.is_ai_turn:
                    app.turn_manager.next_turn()
    except KeyboardInterrupt:
        interrupted_shutdown = True
        agent_client = getattr(app, "agent_client", None)
        if agent_client is not None:
            agent_client._user_cancelled = True
            cancel_event = getattr(agent_client, "_cancel_event", None)
            if cancel_event is not None and hasattr(cancel_event, "set"):
                cancel_event.set()
        app.system_layer.show_muted_message(MSG_SHUTDOWN)
    finally:
        if _pending_async_slot:
            app.runtime_state.decrement_chat_inflight(app._refresh_parallel_toolbar)
            app.runtime_state.release_chat_slot()
            _pending_async_slot = False
        leaked_slots = app.runtime_state.get_chat_inflight_count()
        if leaked_slots > 0:
            app._file_bug(
                session_id=getattr(app.storage, "session_id", ""),
                category="slot_leak_suspect",
                summary=f"Shutdown iniciou com {leaked_slots} slot(s) ainda em uso",
                severity="high",
                confidence=0.9,
            )
            lock = getattr(app.runtime_state, "chat_inflight_lock", None)
            if lock is not None:
                with lock:
                    app.runtime_state.chat_inflight_count = 0
            else:
                app.runtime_state.chat_inflight_count = 0
        if interrupted_shutdown:
            supervisor = getattr(app, "process_supervisor", None)
            if supervisor is not None:
                supervisor.shutdown()
        try:
            if threaded_chat and chat_queue is not None:
                chat_queue.put(None)
            if chat_worker is not None:
                chat_worker.join(timeout=0.5)
            if chat_executor is not None:
                if interrupted_shutdown:
                    chat_executor.shutdown(wait=False, cancel_futures=True)
                    _join_executor_threads(chat_executor, timeout=0.3)
                else:
                    chat_executor.shutdown(wait=True, cancel_futures=False)
        except KeyboardInterrupt:
            pass
        app.runtime_state.chat_executor = None
        app.runtime_state.chat_slot_semaphore = None
        app.runtime_state.chat_queue = None
        app._refresh_parallel_toolbar()

        _agent_client = getattr(app, "agent_client", None)
        if _agent_client is not None:
            _agent_client.close()

        _process_supervisor = getattr(app, "process_supervisor", None)
        if _process_supervisor is not None:
            _process_supervisor.shutdown()

        try:
            app.session_services.shutdown(interrupted=interrupted_shutdown)
            if hasattr(app, "current_job_id") and app.current_job_id is not None:
                TodoRegistry.cleanup(app.current_job_id)
            renderer = getattr(app, "renderer", None)
            if renderer is not None and hasattr(renderer, "close"):
                renderer.close()
            app._run_render_bug_detector()
            if hasattr(app, "behavior_metrics"):
                app.behavior_metrics._flush_if_dirty()
        finally:
            restore_job_env = getattr(app, "_restore_current_job_env", None)
            if callable(restore_job_env):
                restore_job_env()
            bug_store = getattr(app, "bug_store", None)
            if bug_store is not None and hasattr(bug_store, "close"):
                try:
                    bug_store.close()
                except Exception:
                    pass
            _tty.restore_control_echo()


def _join_executor_threads(executor, timeout=2.0):
    """Aguarda threads do executor para evitar travar no atexit."""
    try:
        threads = list(getattr(executor, "_threads", []))
        if not threads:
            return
        deadline = time.monotonic() + timeout
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
    except Exception:
        pass
