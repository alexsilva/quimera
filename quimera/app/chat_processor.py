"""Processamento do loop de chat interativo do QuimeraApp."""

from __future__ import annotations

import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agents.capabilities import mark_user_cancelled
from .config import logger
from .welcome_presenter import WelcomePresenter
from .lifecycle import AppLifecycle
from .tty_control import TtyController
from .session_bootstrap import (
    resolve_render_debug_log_path,
    resolve_session_log_path,
)
from .submission_tracker import new_submission_id, submission_id_of
from .turn import TurnManager
from .worker import ChatWorker, ChatWorkItem
from ..runtime.tools.mcp_clients import get_bridge as get_mcp_client_bridge
from ..runtime.drivers.tool_schemas import get_bridge_schemas
from ..constants import (
    CMD_ALIASES,
    CMD_EDIT,
    CMD_EXIT,
    CMD_FILE_PREFIX,
    MSG_CHAT_STARTED,
    MSG_SESSION_STATUS,
    MSG_SHUTDOWN,
)


_tty = TtyController()

_NORMAL_SHUTDOWN_GRACE_SECONDS = 10.0
_FORCED_SHUTDOWN_JOIN_SECONDS = 0.5


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
    app.renderer.show_banner(WelcomePresenter.build_welcome_message())
    workspace = getattr(app, "workspace", None)
    project_path = str(getattr(workspace, "cwd", Path.cwd()))
    _show_neutral = getattr(
        app.renderer,
        "show_boot_message",
        app.renderer.show_system_neutral,
    )
    _show_neutral(f"Projeto: {project_path}")
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
    mcp_client_bridge = get_mcp_client_bridge()
    if mcp_client_bridge is not None:
        schemas = get_bridge_schemas()
        if schemas:
            _show_neutral(
                f"MCP client ativo: {len(schemas)} tools disponíveis "
                f"({len(mcp_client_bridge.sessions)} conexão(ões))"
            )
        else:
            _show_neutral("MCP client: conectado mas nenhuma tool exposta pelo servidor")
    if getattr(app, "debug_prompt_metrics", False):
        session_log_path = resolve_session_log_path(app.storage, app.workspace)
        if session_log_path:
            _show_neutral(app._format_session_log_message(session_log_path))
        render_debug_log_path = resolve_render_debug_log_path(
            app.storage, app.workspace, app.debug_prompt_metrics
        )
        if render_debug_log_path:
            _show_neutral(f"Audit de render:\n  {render_debug_log_path}\n")
    app.renderer.flush()
    app.renderer.signal_restore_history()

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
    # O plano de controle (leitura de input/comandos) roda sempre em modo assíncrono,
    # independente de --threads. Isso mantém o loop principal responsivo a comandos
    # mesmo com capacidade de execução 1 (--threads 1): a mensagem do usuário é
    # despachada para o ChatWorker/executor em background e o loop segue lendo input.
    # A concorrência real de agentes continua limitada por async_capacity abaixo.
    threaded_chat = True
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
    forced_shutdown = False
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
                    _ai_turn_input = None
                    try:
                        _ai_turn_input = app.read_user_input("", timeout=0)
                    except Exception:
                        pass
                    if _ai_turn_input is not None:
                        _stripped_ai_turn_input = _ai_turn_input.strip()
                        _resolved_cmd = CMD_ALIASES.get(_stripped_ai_turn_input, _stripped_ai_turn_input)
                        if _resolved_cmd.startswith("/"):
                            _cmd_result = app.handle_command(_ai_turn_input)
                            if _cmd_result is not True and not getattr(app, "_turn_blocked_warning_shown", False):
                                app.renderer.show_system("[Aguardando resposta do agente...]")
                                app._turn_blocked_warning_shown = True
                        elif not getattr(app, "_turn_blocked_warning_shown", False):
                            app.renderer.show_system("[Aguardando resposta do agente...]")
                            app._turn_blocked_warning_shown = True
                    elif not getattr(app, "_turn_blocked_warning_shown", False):
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
                    outstanding = app.runtime_state.get_chat_outstanding_count()
                    has_active_work = getattr(app.agent_client, "has_active_work", None)
                    draining = bool(callable(has_active_work) and has_active_work())
                    if (outstanding > 0 or draining) and not ctrl_c_cancelled:
                        ctrl_c_cancelled = True
                        chat_lifecycle.handle_local_interrupt()
                        swallow_threaded_input_interrupt = True
                        continue
                raise
            if user is None:
                if not sys.stdin.isatty():
                    break
                continue

            submission_id = submission_id_of(user)

            if user == CMD_EXIT:
                break

            if user.strip() == CMD_EDIT:
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

            _cmd_result = _handle_command_safely(app, user)
            if _cmd_result is True:
                chat_lifecycle.update_submission_status(submission_id, "completed")
                continue
            elif isinstance(_cmd_result, str):
                user = _cmd_result

            # A Textual já entrega SubmittedInput correlacionado. O CLI clássico
            # entrega str puro; gere o mesmo identificador antes de enfileirar para
            # que um prompt seguinte não consiga apagar semanticamente o
            # cancelamento de uma rodada anterior via reset_cancel_state().
            if not submission_id:
                submission_id = new_submission_id()
                chat_lifecycle.register_submission(submission_id)

            session_state_manager.advance_turn()

            if chat_queue is not None:
                acquired_async_slot = False
                if chat_slot_semaphore is not None:
                    acquired_async_slot = chat_slot_semaphore.acquire(blocking=False)
                if acquired_async_slot:
                    app.runtime_state.increment_chat_inflight(app._refresh_parallel_toolbar)
                    chat_lifecycle.update_submission_status(submission_id, "queued")
                    if (
                        hasattr(app, "turn_manager")
                        and app.turn_manager.is_human_turn
                    ):
                        app.turn_manager.next_turn()
                    _pending_async_slot = True
                    chat_queue.put(
                        ChatWorkItem(
                            user,
                            slot_reserved=True,
                            submission_id=submission_id,
                        )
                    )
                    _pending_async_slot = False
                    app._refresh_parallel_toolbar()
                    deadline = time.monotonic() + 0.05
                    while not chat_queue.empty() and time.monotonic() < deadline:
                        time.sleep(0.001)
                else:
                    if hasattr(app, "turn_manager") and app.turn_manager.is_human_turn:
                        app.turn_manager.next_turn()
                    queue_position = app.runtime_state.increment_chat_pending(
                        app._refresh_parallel_toolbar
                    )
                    chat_lifecycle.update_submission_status(
                        submission_id,
                        "queued",
                        queue_position=queue_position,
                    )
                    chat_queue.put(
                        ChatWorkItem(
                            user,
                            slot_reserved=False,
                            submission_id=submission_id,
                        )
                    )
                    app._refresh_parallel_toolbar()
            else:
                if hasattr(app, "turn_manager"):
                    app.turn_manager.next_turn()
                try:
                    chat_lifecycle.process_message(user, submission_id=submission_id)
                except KeyboardInterrupt:
                    chat_lifecycle.handle_local_interrupt()
                    continue
                if hasattr(app, "turn_manager") and app.turn_manager.is_ai_turn:
                    app.turn_manager.next_turn()
    except KeyboardInterrupt:
        interrupted_shutdown = True
        agent_client = getattr(app, "agent_client", None)
        if agent_client is not None:
            mark_user_cancelled(agent_client)
        app.system_layer.show_muted_message(MSG_SHUTDOWN)
    finally:
        if _pending_async_slot:
            app.runtime_state.decrement_chat_inflight(app._refresh_parallel_toolbar)
            app.runtime_state.release_chat_slot()
            _pending_async_slot = False
        if interrupted_shutdown:
            _process_supervisor = getattr(app, "process_supervisor", None)
            if _process_supervisor is not None:
                _process_supervisor.shutdown()
        try:
            shutdown_deadline = (
                None
                if interrupted_shutdown
                else time.monotonic() + _NORMAL_SHUTDOWN_GRACE_SECONDS
            )
            if threaded_chat and chat_queue is not None:
                chat_queue.put(None)
            if chat_worker is not None:
                chat_worker.join(
                    timeout=(
                        0.5
                        if interrupted_shutdown
                        else _remaining_shutdown_time(shutdown_deadline)
                    )
                )
            if chat_executor is not None:
                if interrupted_shutdown:
                    chat_executor.shutdown(wait=False, cancel_futures=True)
                    _join_executor_threads(chat_executor, timeout=0.3)
                else:
                    # Preserva prompts rápidos já aceitos, mas não deixa `/exit`
                    # bloquear indefinidamente por uma API ou CLI presa.
                    chat_executor.shutdown(wait=False, cancel_futures=False)
                    executor_stopped = _join_executor_threads(
                        chat_executor,
                        timeout=_remaining_shutdown_time(shutdown_deadline),
                    )
                    worker_stopped = chat_worker is None or not chat_worker.is_alive()
                    if not executor_stopped or not worker_stopped:
                        forced_shutdown = True
                        chat_executor.shutdown(wait=False, cancel_futures=True)
                        _cancel_chat_work_for_shutdown(app)
                        if chat_worker is not None:
                            chat_worker.join(timeout=_FORCED_SHUTDOWN_JOIN_SECONDS)
                        _join_executor_threads(
                            chat_executor,
                            timeout=_FORCED_SHUTDOWN_JOIN_SECONDS,
                        )
            if not interrupted_shutdown:
                # Futures concluídos podem ter publicado RenderEvents depois da
                # última iteração do loop. Drena-os antes de desmontar renderer,
                # sessão e bridge.
                chat_lifecycle.drain_ui_events(_ui_event_queue)
                if hasattr(app, "event_sink") and app.event_sink is not None:
                    app.event_sink.drain_pending()
        except KeyboardInterrupt:
            pass
        leaked_slots = app.runtime_state.get_chat_outstanding_count()
        if leaked_slots > 0 and forced_shutdown:
            app._file_bug(
                session_id=getattr(app.storage, "session_id", ""),
                category="slot_leak_suspect",
                summary=(
                    "Shutdown forçado após o prazo com "
                    f"{leaked_slots} prompt(s) ainda pendente(s)"
                ),
                severity="high",
                confidence=0.95,
            )
        _reset_chat_work_counts(app.runtime_state)
        app.runtime_state.chat_executor = None
        app.runtime_state.chat_slot_semaphore = None
        app.runtime_state.chat_queue = None
        app.runtime_state.chat_pending_count = 0
        app._refresh_parallel_toolbar()
        try:
            lifecycle = getattr(app, "lifecycle", None)
            if lifecycle is None:
                lifecycle = AppLifecycle(app)
                app.lifecycle = lifecycle
            lifecycle.close(interrupted=interrupted_shutdown or forced_shutdown)
        finally:
            _tty.restore_control_echo()


def _handle_command_safely(app, user_input: str):
    """Executa `handle_command` isolando falhas do comando do loop de chat.

    Uma exception dentro de um handler de comando não deve derrubar a aplicação:
    o erro é registrado, exibido como aviso no feed e a entrada é considerada
    consumida (retorna ``True``) para não vazar o comando como mensagem de chat.
    """
    try:
        return app.handle_command(user_input)
    except KeyboardInterrupt:
        raise
    except Exception as error:
        logger.exception("falha ao processar comando: %s", user_input)
        display = getattr(app, "system_layer", app)
        show_warning_message = getattr(display, "show_warning_message", None)
        if callable(show_warning_message):
            show_warning_message(f"[erro] falha ao processar comando: {error}")
        return True


def _remaining_shutdown_time(deadline: float | None) -> float:
    if deadline is None:
        return 0.0
    return max(0.0, deadline - time.monotonic())


def _cancel_chat_work_for_shutdown(app) -> None:
    agent_client = getattr(app, "agent_client", None)
    cancel_active_work = getattr(agent_client, "cancel_active_work", None)
    if callable(cancel_active_work):
        cancel_active_work()
    else:
        mark_user_cancelled(agent_client)
    process_supervisor = getattr(app, "process_supervisor", None)
    terminate_all = getattr(process_supervisor, "terminate_all", None)
    if callable(terminate_all):
        terminate_all()


def _reset_chat_work_counts(runtime_state) -> None:
    lock = getattr(runtime_state, "chat_inflight_lock", None)
    if lock is not None:
        with lock:
            runtime_state.chat_inflight_count = 0
            runtime_state.chat_pending_count = 0
        return
    runtime_state.chat_inflight_count = 0
    runtime_state.chat_pending_count = 0


def _join_executor_threads(executor, timeout=2.0) -> bool:
    """Aguarda threads do executor e informa se todas terminaram."""
    try:
        threads = list(getattr(executor, "_threads", []))
        if not threads:
            return True
        deadline = time.monotonic() + timeout
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        return not any(thread.is_alive() for thread in threads)
    except Exception:
        return False
