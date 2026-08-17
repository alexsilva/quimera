"""AgentClient: orquestra chamadas a agentes externos (CLI e API)."""
import json
import hashlib
import logging
import os
import queue
import threading
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import quimera.profiles as profiles
from quimera.constants import MAX_STDERR_LINES, Visibility
from quimera.profiles.base import CliConnection, OpenAIConnection
from quimera import process_factory as subprocess
from quimera.sandbox.bwrap import build_bwrap_cmd
from quimera.spy_output_presenter import SpyOutputPresenter
from quimera.runtime.tool_preview import ToolPreview
from quimera.prompt_templates import PromptText

from quimera.agents.parsers import parse_stream_json, parse_codex_json, parse_opencode_json
from quimera.agents.process_runner import ProcessRunner, MAX_WALL_CLOCK_SECONDS
from quimera.agents.signal_guard import EscMonitor, terminate_process_group
from quimera.agents.warm_pool import WarmPool
from quimera.runtime.process_supervisor import ProcessSupervisor
from quimera.domain.execution import (
    ExecutionControlEvent,
    ExecutionControlSource,
    ExecutionControlStatus,
)
from quimera.agents.text_filters import (
    _strip_spinner,
    _should_ignore_stderr_line,
    _filter_stderr_lines,
    _is_rate_limit_signal,
)
from quimera.runtime.drivers.openai_compat import (
    APIExecutionError as _APIExecutionError,
    FatalAPIError as _FatalAPIError,
    TransientAPIError as _TransientAPIError,
)

_logger = logging.getLogger(__name__)

OpenAICompatDriver = None


_GUI_VARS = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SYSTEM_BUS_ADDRESS", "XAUTHORITY", "XDG_RUNTIME_DIR",
})


class AgentClient:
    """Executa os agentes externos no diretório de trabalho do projeto."""

    _MAX_STDOUT_CHARS = 128_000
    _MAX_LOG_QUEUE_ITEMS = 512

    def __init__(self, renderer, metrics_file=None, idle_timeout=None, visibility=Visibility.SUMMARY,
                 working_dir=None, workspace_root=None, tool_executor=None, error_reporter=None,
                 muted_reporter=None, session_id=None, workspace_tmp_root=None,
                 process_supervisor=None, pause_idle_if=None):
        """Inicializa uma instância de AgentClient."""
        self.renderer = renderer
        self.error_reporter = error_reporter
        self.muted_reporter = muted_reporter
        self.metrics_file = metrics_file
        self._metrics_lock = threading.Lock()
        self.idle_timeout = idle_timeout
        self._pause_idle_if = pause_idle_if
        self.visibility = Visibility(visibility)
        # `workspace_root` é mantido como alias compatível.
        self.working_dir = working_dir if working_dir is not None else workspace_root
        # Injetado de app.py após criação do ToolExecutor; usado pelos drivers de API.
        self.tool_executor = tool_executor
        # Cache de instâncias de driver por nome de agente.
        self._api_drivers: dict = {}
        self._api_driver_signatures: dict = {}
        self._active_api_runs: dict[object, threading.Event] = {}
        self._active_api_runs_lock = threading.Lock()
        self.tool_event_callback = None
        # Modo de execução ativo; quando definido, subprocessos são envolvidos com bwrap.
        self.execution_mode = None
        self._cancel_event = threading.Event()
        self._owns_cancel_event = True
        self._esc_monitor = EscMonitor(self._cancel_event)
        self._user_cancelled = False
        self._cancel_notice_lock = threading.Lock()
        self._cancel_notice_state = {"shown": False}
        self._call_lock = threading.RLock()
        self._agent_running = False
        self._running_agent = None
        self._current_proc = None
        self._cancel_listeners: list = []
        self.session_id = session_id
        self.workspace_tmp_root = Path(workspace_tmp_root) if workspace_tmp_root is not None else None
        self._spy_output_presenter = SpyOutputPresenter(
            self.renderer,
            self.visibility,
            session_id=self.session_id,
            base_dir=self.workspace_tmp_root,
        )
        self.last_spy_turn_detail: dict | None = None
        self._pending_summary_render: tuple | None = None
        self.rate_limit_detected = False
        self.rate_limit_detected_at: float | None = None
        self.rate_limit_retry_after: float | None = None
        self._warm_pool = WarmPool()
        self.process_supervisor: ProcessSupervisor | None = process_supervisor

    def _show_error(
        self,
        message: str,
        *,
        agent: str | None = None,
        command_name: str | None = None,
        error_kind: str | None = None,
        return_code: int | None = None,
    ) -> None:
        has_structured = not all(v is None for v in (agent, command_name, error_kind, return_code))
        if has_structured:
            try:
                self.renderer.show_error(
                    message,
                    agent=agent,
                    command_name=command_name,
                    error_kind=error_kind,
                    return_code=return_code,
                )
            except TypeError:
                rendered_message = self._format_error_for_reporter(
                    message,
                    agent=agent,
                    command_name=command_name,
                    error_kind=error_kind,
                    return_code=return_code,
                )
                self.renderer.show_error(rendered_message)
            return

        reporter = self.error_reporter
        if callable(reporter):
            rendered_message = self._format_error_for_reporter(
                message,
                agent=agent,
                command_name=command_name,
                error_kind=error_kind,
                return_code=return_code,
            )
            reporter(rendered_message)
            return
        self.renderer.show_error(message)

    @staticmethod
    def _agent_subject(agent: str | None, command_name: str) -> str:
        return (agent or "").strip() or command_name

    @classmethod
    def _format_error_for_reporter(
        cls,
        message: str,
        *,
        agent: str | None = None,
        command_name: str | None = None,
        error_kind: str | None = None,
        return_code: int | None = None,
    ) -> str:
        subject = cls._agent_subject(agent, command_name or "unknown")
        if error_kind == "agent_exit" and return_code is not None:
            return f"[erro] agente {subject} retornou código {return_code}"
        if error_kind == "agent_comm":
            return f"[erro] falha ao comunicar com {subject}: {message}"
        if error_kind == "agent_invalid_output":
            return f"[erro] agente {subject} não retornou saída válida"
        return message

    def _show_muted(self, message: str) -> None:
        reporter = self.muted_reporter
        if callable(reporter):
            reporter(message)
            return
        self.renderer.show_system_neutral(message)

    def _show_tool_preview(self, message: str, *, agent: str | None = None, metadata=None) -> None:
        """Exibe preview operacional de tool no feed quando possível."""
        context = self._tool_preview_context(metadata)
        payload = {"content": message, **context} if context else message
        if self.renderer.supports_agent_feed is True:
            show_tool_preview = getattr(self.renderer, "show_tool_preview", None)
            if callable(show_tool_preview):
                show_tool_preview(payload, agent=agent, metadata=metadata)
            else:
                self.renderer.show_feed(message, agent=agent, muted=True)
            return
        self._show_muted(message)

    def bind_tool_preview_callback(self, tool_executor, *, agent: str | None = None) -> None:
        """Registra o preview operacional compartilhado para tools sem approval."""
        set_tool_preview = getattr(tool_executor, "set_tool_preview_callback", None)
        if callable(set_tool_preview):
            set_tool_preview(
                lambda name, args, metadata=None: self._show_tool_preview(
                    ToolPreview.build(name, args),
                    agent=self._agent_from_tool_metadata(metadata) or agent,
                    metadata=metadata,
                )
            )

    @staticmethod
    def _agent_from_tool_metadata(metadata) -> str | None:
        """Extrai o agente chamador de metadata MCP confiável."""
        if not isinstance(metadata, dict):
            return None
        context = metadata.get("trusted_context")
        agent_name = getattr(context, "agent_name", None)
        if agent_name:
            return str(agent_name)
        state = metadata.get("_mcp_state")
        if isinstance(state, dict) and state.get("agent_name"):
            return str(state["agent_name"])
        server_origin = str(getattr(context, "server_origin", "") or "").strip()
        transport = str(getattr(context, "transport", "") or "").strip()
        if server_origin == "mcp_http" or transport == "http_mcp":
            return "mcp-http"
        if isinstance(state, dict) and state.get("transport") == "http_mcp":
            return "mcp-http"
        return None

    @staticmethod
    def _tool_preview_context(metadata) -> dict[str, str]:
        """Extrai metadados visuais confiáveis de uma chamada de tool."""
        if not isinstance(metadata, dict):
            return {}
        context = metadata.get("trusted_context")
        state = metadata.get("_mcp_state")
        server_origin = str(getattr(context, "server_origin", "") or "").strip()
        transport = str(getattr(context, "transport", "") or "").strip()
        state_transport = str(state.get("transport") or "").strip() if isinstance(state, dict) else ""
        is_http_mcp = (
            server_origin == "mcp_http"
            or transport == "http_mcp"
            or state_transport == "http_mcp"
        )
        if not is_http_mcp:
            return {}
        payload: dict[str, str] = {}
        for attr, key in (
            ("run_id", "run_id"),
            ("parent_run_id", "parent_run_id"),
            ("transport", "transport"),
            ("server_origin", "server_origin"),
            ("session_id", "session_id"),
            ("client_name", "client_name"),
            ("client_version", "client_version"),
            ("http_profile", "http_profile"),
        ):
            value = getattr(context, attr, None)
            if value:
                payload[key] = str(value)
        if payload.get("transport") == "http_mcp":
            payload["transport"] = "mcp_http"
        if isinstance(state, dict):
            client_info = state.get("client_info") or {}
            if isinstance(client_info, dict):
                if client_info.get("name") and not payload.get("client_name"):
                    payload["client_name"] = str(client_info["name"])
                if client_info.get("version") and not payload.get("client_version"):
                    payload["client_version"] = str(client_info["version"])
            if state.get("trace_id"):
                payload["trace_id"] = str(state["trace_id"])
            if state.get("trusted_run_id") and not payload.get("run_id"):
                payload["run_id"] = str(state["trusted_run_id"])
            if state.get("parent_run_id") and not payload.get("parent_run_id"):
                payload["parent_run_id"] = str(state["parent_run_id"])
        if metadata.get("mcp_msg_id") is not None:
            payload["mcp_msg_id"] = str(metadata["mcp_msg_id"])
        return {key: value for key, value in payload.items() if value}

    @staticmethod
    def _is_tool_call_text(text: str) -> bool:
        cleaned = text.strip()
        return (
            cleaned.startswith("tool:")
            or cleaned.startswith("$ ")
            or cleaned.startswith("✓ ")
            or cleaned.startswith("✗ ")
        )

    def reset_cancel_notices(self) -> None:
        """Permite exibir novamente avisos de cancelamento em um novo ciclo."""
        with self._cancel_notice_lock:
            self._cancel_notice_state["shown"] = False
            self._cancel_notice_state.pop("source", None)

    def reset_cancel_state(self) -> None:
        """Limpa cancelamento somente quando este client é dono da rodada.

        Clients que receberam um evento externo por ``share_cancel_event`` são
        consumidores: limpar esse evento permitiria que um fork ou delegate
        revivesse trabalho cancelado por outro fluxo. O dono da rodada continua
        responsável por limpar o evento antes de aceitar uma nova mensagem.
        """
        if not self._owns_cancel_event:
            self._user_cancelled = self._cancel_event.is_set()
            return
        self._user_cancelled = False
        self._cancel_event.clear()
        self.reset_cancel_notices()

    @property
    def user_cancelled(self) -> bool:
        """Indica cancelamento explícito solicitado pelo usuário."""
        return self._user_cancelled

    @user_cancelled.setter
    def user_cancelled(self, value: bool) -> None:
        self._user_cancelled = bool(value)

    @property
    def cancel_event(self) -> threading.Event:
        """Evento cooperativo compartilhado com runners e drivers."""
        return self._cancel_event

    @property
    def agent_running(self) -> bool:
        """Indica se há processo ou driver marcado como em execução."""
        return self._agent_running

    @property
    def pause_idle_if(self):
        """Callback que suspende idle timeout durante operações externas."""
        return self._pause_idle_if

    def share_cancel_event(self, cancel_event: threading.Event) -> None:
        """Compartilha um evento de cancelamento com outro fluxo de execução."""
        if not all(callable(getattr(cancel_event, name, None)) for name in ("set", "clear", "is_set")):
            raise TypeError("cancel_event deve implementar set(), clear() e is_set()")
        self._cancel_event = cancel_event
        self._owns_cancel_event = False
        self._esc_monitor = EscMonitor(self._cancel_event)

    def _fork_tool_executor(self):
        """Cria o ToolExecutor isolado do fork, ou None se não for possível.

        Compartilhar o executor do chat com uma execução concorrente é inseguro:
        cancelamento de approval, spinner e callbacks de tool são reescritos por
        execução. Quando o executor injetado não sabe se isolar, devolvemos None
        em vez de reintroduzir o compartilhamento — o chamador então serializa
        no client primário, que continua correto.
        """
        tool_executor = self.tool_executor
        if tool_executor is None:
            return None
        fork_executor = getattr(tool_executor, "fork_for_concurrent_run", None)
        if not callable(fork_executor):
            return None
        return fork_executor()

    def fork_for_concurrent_run(self) -> "AgentClient | None":
        """Cria um client isolado para uma execução concorrente.

        ``AgentClient.run`` deliberadamente não é reentrante porque mantém estado
        mutável por processo (``_current_proc``, ``_agent_running``, warm pool e
        drivers de API). O fork preserva a configuração operacional do client do
        chat, mas recebe estado de execução próprio — inclusive um
        ``ToolExecutor`` isolado. O evento de cancelamento é compartilhado para
        que Ctrl+C continue cancelando todas as execuções da rodada, inclusive as
        que usam clients isolados.

        Retorna ``None`` quando há um ``tool_executor`` injetado que não sabe se
        isolar: sem executor próprio o fork não é seguro, e o chamador deve
        serializar no client primário.
        """
        forked_tool_executor = self._fork_tool_executor()
        if self.tool_executor is not None and forked_tool_executor is None:
            return None
        forked = AgentClient(
            self.renderer,
            metrics_file=self.metrics_file,
            idle_timeout=self.idle_timeout,
            visibility=self.visibility,
            working_dir=self.working_dir,
            tool_executor=forked_tool_executor,
            error_reporter=self.error_reporter,
            muted_reporter=self.muted_reporter,
            session_id=self.session_id,
            workspace_tmp_root=self.workspace_tmp_root,
            process_supervisor=self.process_supervisor,
            pause_idle_if=self._pause_idle_if,
        )
        forked.execution_mode = self.execution_mode
        forked.tool_event_callback = self.tool_event_callback
        forked.share_cancel_event(self._cancel_event)
        forked._cancel_notice_lock = self._cancel_notice_lock
        forked._cancel_notice_state = self._cancel_notice_state
        forked._active_api_runs = self._active_api_runs
        forked._active_api_runs_lock = self._active_api_runs_lock
        return forked

    def has_active_work(self) -> bool:
        """Indica trabalho ainda vivo, inclusive chamadas API abandonadas pelo chamador."""
        if self._agent_running:
            return True
        with self._active_api_runs_lock:
            return bool(self._active_api_runs)

    def _register_api_run(self, token: object, cancel_event: threading.Event) -> None:
        with self._active_api_runs_lock:
            self._active_api_runs[token] = cancel_event

    def _unregister_api_run(self, token: object) -> None:
        with self._active_api_runs_lock:
            self._active_api_runs.pop(token, None)

    def _cancel_active_api_runs(self) -> None:
        with self._active_api_runs_lock:
            cancel_events = list(self._active_api_runs.values())
        for cancel_event in cancel_events:
            cancel_event.set()

    def _is_expected_termination_return_code(self, return_code) -> bool:
        """Retorna True para SIGTERM decorrente de cancelamento controlado."""
        if return_code not in {-15, 143}:
            return False
        return self._cancel_event.is_set() or self._user_cancelled

    def _show_cancelled_once(self) -> None:
        """Publica uma única confirmação estruturada de cancelamento."""
        should_show = False
        with self._cancel_notice_lock:
            if not self._cancel_notice_state["shown"]:
                self._cancel_notice_state["shown"] = True
                should_show = True
            source = self._cancel_notice_state.get("source") or ExecutionControlSource.USER
        if should_show:
            self.renderer.show_execution_control(
                ExecutionControlEvent(
                    status=ExecutionControlStatus.CANCELLED,
                    source=source,
                    agent=self._running_agent,
                )
            )

    # ------------------------------------------------------------------
    # Helpers de sinal (delegam para EscMonitor para retrocompatibilidade)
    # ------------------------------------------------------------------

    def _start_esc_monitor(self) -> None:
        self._esc_monitor.start()

    def _stop_esc_monitor(self) -> None:
        self._agent_running = False
        self._esc_monitor.stop()

    def _terminate_process_group(self, proc) -> None:
        terminate_process_group(proc)

    def is_cancelled(self) -> bool:
        """Retorna True se o trabalho ativo foi cancelado."""
        return self._cancel_event.is_set() or self._user_cancelled

    def add_cancel_listener(self, listener) -> None:
        """Registra callback disparado quando o trabalho ativo é cancelado.

        Usado para propagar o cancelamento do usuário a AgentClients de
        background (delegações), que possuem cancel_event próprio.
        """
        self._cancel_listeners.append(listener)

    def _notify_cancel_listeners(self) -> None:
        for listener in list(self._cancel_listeners):
            try:
                listener()
            except Exception:
                _logger.debug("cancel listener falhou", exc_info=True)

    def cancel_active_work(
        self, source: ExecutionControlSource = ExecutionControlSource.USER
    ) -> None:
        """Cancela o trabalho atual e encerra subprocessos ainda vivos.

        `source` distingue cancelamento do usuário (Esc, /debate cancel) de
        cancelamento do sistema (timeout de debate) na notificação exibida.
        """
        self._user_cancelled = True
        with self._cancel_notice_lock:
            self._cancel_notice_state.setdefault("source", source)
        self._cancel_event.set()
        self._cancel_active_api_runs()
        # Listeners primeiro: clients de background precisam do cancel_event
        # setado antes do SIGTERM chegar, para tratar -15 como término esperado.
        self._notify_cancel_listeners()
        current_proc = self._current_proc
        if current_proc is not None:
            try:
                self._terminate_process_group(current_proc)
            except Exception:
                _logger.debug("cancel_active_work: falha ao terminar current_proc", exc_info=True)
        if self.process_supervisor is not None:
            try:
                self.process_supervisor.terminate_all()
            except Exception:
                _logger.debug("cancel_active_work: falha ao terminar processos supervisionados", exc_info=True)

    # ------------------------------------------------------------------
    # Formatação de stdout ao vivo
    # ------------------------------------------------------------------

    def _show_formatted_stdout(self, agent: str | None, line: str) -> bool:
        """Exibe mensagens resumidas de stdout quando o profile oferece formatter."""
        return self._spy_output_presenter.consume_stdout(agent, line)

    def _render_agent_transient(self, message: str, *, agent: str | None, muted: bool = False) -> None:
        """Renderiza linha ao vivo do agente priorizando a janela transient rolante."""
        if agent and (muted or self.renderer.supports_agent_feed is True):
            self.renderer.update_agent_transient(agent, message)
            return
        if muted:
            self.renderer.show_plain(message, agent=agent, muted=True)
        else:
            self.renderer.show_plain(message, agent=agent)

    @classmethod
    def _append_capped_stdout(cls, result_holder: dict, chunk: str) -> None:
        """Mantém apenas a cauda recente de stdout para evitar retenção ilimitada."""
        chunks = result_holder["stdout_chunks"]
        chunks.append(chunk)
        result_holder["stdout_total"] += len(chunk)

        while chunks and result_holder["stdout_total"] > cls._MAX_STDOUT_CHARS:
            removed = chunks.popleft()
            result_holder["stdout_total"] -= len(removed)
            result_holder["stdout_truncated"] = True

    @staticmethod
    def _get_capped_stdout(result_holder: dict) -> str:
        """Retorna stdout concatenado com marcador quando houve descarte de prefixo."""
        output = "".join(result_holder["stdout_chunks"])
        if result_holder["stdout_truncated"]:
            return "[...stdout truncado...]\n" + output
        return output

    @classmethod
    def _enqueue_log_item(cls, log_queue, item) -> None:
        """Enfileira saída ao vivo com descarte do item mais antigo sob pressão."""
        if log_queue is None:
            return
        try:
            log_queue.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            log_queue.get_nowait()
        except queue.Empty:
            return

        try:
            log_queue.put_nowait(item)
        except queue.Full:
            return

    # ------------------------------------------------------------------
    # Helpers de ambiente e comando
    # ------------------------------------------------------------------

    @staticmethod
    def _build_run_env(extra_env=None) -> dict:
        """Constrói o ambiente de execução, filtrando variáveis de GUI."""
        env = {k: v for k, v in os.environ.items() if k not in _GUI_VARS}
        env.update({"NO_COLOR": "1", "TERM": "dumb", "COLORTERM": ""})
        if extra_env:
            env.update(extra_env)
        return env

    def _build_effective_cmd(self, cmd: list, agent: str | None, cwd: str | None) -> tuple[list, str | None]:
        """Resolve o comando efetivo, aplicando bwrap se necessário."""
        effective_cwd = cwd or self.working_dir
        if self.execution_mode is not None and effective_cwd:
            effective_cmd = build_bwrap_cmd(
                self.execution_mode,
                effective_cwd,
                cmd,
                profile=profiles.get(agent) if agent else None,
            )
            return effective_cmd, effective_cwd
        return list(cmd), effective_cwd

    # ------------------------------------------------------------------
    # run() — execução de subprocess
    # ------------------------------------------------------------------

    def run(
        self,
        cmd,
        input_text=None,
        silent=False,
        agent=None,
        show_status=True,
        extra_env=None,
        cwd=None,
        _primed_proc=None,
        progress_callback=None,
    ):
        """Executa um comando (agente CLI) e retorna o stdout completo."""
        if self._agent_running:
            # run() não é reentrante: um segundo run sobre o mesmo client
            # limpa cancel_event/_user_cancelled do run ativo, sobrescreve
            # _current_proc e para o EscMonitor ao terminar. Delegações
            # concorrentes devem usar um AgentClient isolado (background
            # dispatch). O log alto garante que a regressão nunca seja muda.
            _logger.error(
                "AgentClient.run() reentrante: '%s' iniciado enquanto '%s' ainda executa "
                "neste client; use o dispatch de background isolado para delegações",
                agent or (cmd[0] if cmd else "?"),
                self._running_agent or "?",
            )
            return None
        if self._cancel_event.is_set():
            self._user_cancelled = True
            self._show_cancelled_once()
            return None
        self._user_cancelled = False
        self.rate_limit_detected = False
        self.rate_limit_detected_at = None
        self.rate_limit_retry_after = None
        self._agent_running = True
        self._running_agent = agent or (cmd[0] if cmd else None)
        self._start_esc_monitor()
        env = self._build_run_env(extra_env)
        effective_cmd, effective_cwd = self._build_effective_cmd(cmd, agent, cwd)
        if _primed_proc is not None and _primed_proc.poll() is None:
            proc = _primed_proc
            _logger.debug("[warm-pool] reutilizando processo pré-aquecido: %s", cmd[0])
        else:
            if _primed_proc is not None:
                _logger.debug("[warm-pool] processo pré-aquecido expirou: %s", cmd[0])
            try:
                proc = subprocess.popen_text(
                    effective_cmd,
                    env=env,
                    cwd=effective_cwd,
                    start_new_session=True,
                )
            except OSError as exc:
                self._agent_running = False
                self._running_agent = None
                self._stop_esc_monitor()
                self._show_error(f"[erro] não foi possível iniciar {cmd[0]}: {exc}")
                return None
        self._current_proc = proc
        if self.process_supervisor is not None:
            self.process_supervisor.register(proc, owner=agent or "cli", label=cmd[0] if cmd else None)

        result_holder = {
            "stdout_chunks": deque(),
            "stdout_total": 0,
            "stdout_truncated": False,
            "stderr": deque(maxlen=MAX_STDERR_LINES * 2),
            "error": None,
        }
        log_queue = queue.Queue(maxsize=self._MAX_LOG_QUEUE_ITEMS) if not silent else None
        stderr_lines_shown = 0
        self._spy_output_presenter.reset()

        def _read_stdout():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        self._append_capped_stdout(result_holder, line)
                        if log_queue is not None and self.visibility in {Visibility.SUMMARY, Visibility.FULL}:
                            self._enqueue_log_item(log_queue, ("stdout", line))
            except Exception as exc:
                result_holder["error"] = exc

        def _read_stderr():
            try:
                if proc.stderr:
                    for line in proc.stderr:
                        result_holder["stderr"].append(line)
                        if log_queue is not None:
                            self._enqueue_log_item(log_queue, ("stderr", line))
            except Exception as exc:
                result_holder["error"] = exc

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            if input_text and proc.stdin:
                proc.stdin.write(input_text)
                proc.stdin.flush()
            if proc.stdin:
                proc.stdin.close()
        except Exception as exc:
            self._agent_running = False
            self._running_agent = None
            self._stop_esc_monitor()
            self._show_error(f"[erro] falha ao enviar input para {cmd[0]}: {exc}")
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # O fallback é deliberadamente no PID, nunca no grupo do processo.
                try:
                    proc.kill()
                except OSError:
                    pass
            except Exception:
                pass
            return None

        runner = ProcessRunner(
            proc, stdout_thread, stderr_thread, result_holder,
            self._cancel_event, self.idle_timeout,
            pause_idle_if=self._pause_idle_if,
        )

        def _pump_tool_input_once() -> bool:
            """Processa aprovação/ask_user pendente enquanto aguardamos CLI.

            Agentes CLI com MCP podem bloquear esperando a resposta de uma
            tool. Se essa tool pede aprovação humana, o pedido fica na fila do
            InputBroker e precisa ser processado pela thread principal que está
            justamente dentro deste watchdog.
            """
            executor = self.tool_executor
            process_input = getattr(executor, "process_pending_input_once", None)
            if not callable(process_input):
                return False
            try:
                return bool(process_input())
            except Exception:
                _logger.debug("CLI input pump failed", exc_info=True)
                return False

        try:
            if silent:
                def _on_tick_silent(elapsed):
                    _pump_tool_input_once()
                    if progress_callback:
                        progress_callback(f"aguardando resposta de {agent or cmd[0]}... {elapsed}s")

                termination = runner.watch(on_tick=_on_tick_silent)
                self.rate_limit_detected = runner.rate_limit_detected
                self.rate_limit_detected_at = runner.rate_limit_detected_at

                if termination == ProcessRunner.CANCELLED:
                    self.cancel_active_work()
                    self._agent_running = False
                    self._current_proc = None
                    self._stop_esc_monitor()
                    self._show_cancelled_once()
                    return None
                if termination == ProcessRunner.RATE_LIMIT:
                    self._agent_running = False
                    self._current_proc = None
                    self._stop_esc_monitor()
                    _logger.warning("[rate limit] %s em espera; cedendo para outros agentes", cmd[0])
                    return None
                if termination == ProcessRunner.TIMEOUT:
                    self._agent_running = False
                    self._current_proc = None
                    self._stop_esc_monitor()
                    _logger.warning(
                        "[erro] idle timeout after %ds without stdout for %s",
                        self.idle_timeout,
                        cmd[0],
                    )
                    return None
                if termination == ProcessRunner.WALL_TIMEOUT:
                    self._agent_running = False
                    self._current_proc = None
                    self._stop_esc_monitor()
                    _logger.warning(
                        "[erro] wall-clock timeout for %s (limit %ds)",
                        cmd[0], runner._max_wall_clock,
                    )
                    return None

                debug_output = self._get_capped_stdout(result_holder)
                if debug_output:
                    _logger.debug(debug_output)
                filtered_stderr = _filter_stderr_lines(agent, list(result_holder["stderr"]))
                if filtered_stderr:
                    _logger.warning("".join(filtered_stderr))

            else:
                assert log_queue is not None
                with nullcontext(None):
                    _first_stdout_seen = [False]

                    def _on_item(stream_type, line):
                        nonlocal stderr_lines_shown
                        if stream_type == "stderr" and _is_rate_limit_signal(line):
                            runner.notify_rate_limit()
                        cleaned = _strip_spinner(line.rstrip("\n"))
                        if not cleaned.strip():
                            if show_status:
                                _lbl = self._spy_output_presenter.compose_status_label(cmd[0])
                                self.renderer.update_agent_transient(agent or cmd[0], _lbl)
                            return
                        if stream_type == "stdout":
                            _first_stdout_seen[0] = True
                            if self.visibility in {Visibility.SUMMARY, Visibility.FULL}:
                                self._show_formatted_stdout(agent, cleaned)
                            return
                        if stream_type == "stderr" and _should_ignore_stderr_line(agent, line):
                            return
                        self._spy_output_presenter.flush(agent)
                        if show_status:
                            _lbl = self._spy_output_presenter.compose_status_label(cmd[0])
                            self.renderer.update_agent_transient(agent or cmd[0], _lbl)
                        _stderr_limit = MAX_STDERR_LINES * 5
                        if stderr_lines_shown < _stderr_limit:
                            if self._is_tool_call_text(cleaned):
                                self._render_agent_transient(cleaned, agent=agent, muted=True)
                            else:
                                self._render_agent_transient(cleaned, agent=agent)
                        elif stderr_lines_shown == _stderr_limit:
                            self._render_agent_transient(
                                f"... (stderr truncado, máximo {_stderr_limit} linhas)", agent=agent)
                        stderr_lines_shown += 1

                    def _on_tick(elapsed):
                        _pump_tool_input_once()
                        if progress_callback:
                            progress_callback(f"aguardando resposta de {agent or cmd[0]}...")

                    self._spy_output_presenter.notify_agent_started(agent)
                    termination = runner.watch(log_queue=log_queue, on_item=_on_item, on_tick=_on_tick)
                    self.renderer.clear_agent_transient(agent or cmd[0])
                    self.rate_limit_detected = runner.rate_limit_detected
                    self.rate_limit_detected_at = runner.rate_limit_detected_at

                    if termination == ProcessRunner.CANCELLED:
                        self.cancel_active_work()
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        self._show_cancelled_once()
                        return None
                    if termination == ProcessRunner.RATE_LIMIT:
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        self._show_error(
                            f"[rate limit] {cmd[0]} em espera; cedendo para outros agentes")
                        return None
                    if termination == ProcessRunner.TIMEOUT:
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        self._show_error(
                            f"[erro] idle timeout after {self.idle_timeout}s without stdout for {cmd[0]}")
                        return None
                    if termination == ProcessRunner.WALL_TIMEOUT:
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        self._show_error(
                            f"[erro] wall-clock timeout for {cmd[0]} (limit {runner._max_wall_clock}s)")
                        return None

            self._spy_output_presenter.flush(agent)
            proc.wait()
            if not silent and self.visibility == Visibility.SUMMARY and proc.returncode == 0 and not self._cancel_event.is_set():
                if agent:
                    self.renderer.show_plain("execução concluída", agent=agent, muted=True)
                else:
                    self._show_muted(f"← {cmd[0]} concluído")
            if result_holder["error"]:
                self._show_error(
                    str(result_holder["error"]),
                    agent=agent,
                    command_name=cmd[0],
                    error_kind="agent_comm",
                )
                return None

            output = self._get_capped_stdout(result_holder).strip()
            error = "".join(_filter_stderr_lines(agent, list(result_holder["stderr"]))).strip()
        finally:
            if self.process_supervisor is not None:
                self.process_supervisor.unregister(proc)
            should_render_turn_summary = not silent and self.visibility in {Visibility.SUMMARY, Visibility.FULL}
            self.last_spy_turn_detail = self._spy_output_presenter.finalize_turn(
                agent,
                render_summary=False,
            )
            self._pending_summary_render = (agent, self.last_spy_turn_detail, should_render_turn_summary)
            self._agent_running = False
            self._running_agent = None
            self._stop_esc_monitor()
            self._spy_output_presenter.reset()

        if proc.returncode != 0:
            if self._is_expected_termination_return_code(proc.returncode):
                return None
            self._show_error(
                f"[erro] retornou código {proc.returncode}",
                agent=agent,
                command_name=cmd[0],
                error_kind="agent_exit",
                return_code=proc.returncode,
            )
            if error and (silent or agent):
                tail = "\n".join(error.splitlines()[-5:])
                if self._is_tool_call_text(tail):
                    self.renderer.show_plain(tail, agent=agent, muted=True)
                else:
                    self.renderer.show_plain(tail, agent=agent)
            return None

        if not output:
            if error:
                self._show_error(
                    "",
                    agent=agent,
                    command_name=cmd[0],
                    error_kind="agent_invalid_output",
                )
                if silent or agent:
                    tail = "\n".join(error.splitlines()[-5:])
                    if self._is_tool_call_text(tail):
                        self.renderer.show_plain(tail, agent=agent, muted=True)
                    else:
                        self.renderer.show_plain(tail, agent=agent)
            return None

        return output

    # ------------------------------------------------------------------
    # Parser wrappers (mantidos para retrocompatibilidade e testes)
    # ------------------------------------------------------------------

    def _parse_stream_json(self, raw: str, agent: str) -> str | None:
        return parse_stream_json(raw, agent, self.tool_event_callback)

    def _parse_codex_json(self, raw: str, agent: str) -> str | None:
        return parse_codex_json(raw, agent, self.tool_event_callback)

    def _parse_opencode_json(self, raw: str, agent: str) -> str | None:
        return parse_opencode_json(raw, agent, self.tool_event_callback)

    # ------------------------------------------------------------------
    # Profile resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_profile_connection(profile):
        """Resolve a conexão efetiva com fallback para objetos profile simplificados."""
        resolver = getattr(profile, "effective_connection", None)
        if callable(resolver):
            connection = resolver()
            if isinstance(connection, (OpenAIConnection, CliConnection)):
                return connection
        driver = getattr(profile, "driver", "cli")
        if isinstance(driver, str) and driver != "cli":
            return OpenAIConnection(
                model=getattr(profile, "model", None) or "gpt-4o",
                base_url=getattr(profile, "base_url", None) or "https://api.openai.com/v1",
                api_key_env=getattr(profile, "api_key_env", None),
                provider=driver,
                supports_native_tools=getattr(profile, "supports_tools", True),
            )
        return CliConnection(
            cmd=list(getattr(profile, "cmd", None) or []),
            prompt_as_arg=getattr(profile, "prompt_as_arg", False),
            output_format=getattr(profile, "output_format", None),
        )

    @staticmethod
    def _resolve_profile_cli_attrs(profile, connection) -> tuple[list[str], bool, str | None]:
        """Resolve atributos CLI com fallback para profiles simplificados em testes."""
        if isinstance(connection, CliConnection):
            cmd_resolver = getattr(profile, "effective_cmd", None)
            cmd = cmd_resolver() if callable(cmd_resolver) else list(connection.cmd)
            output_format = connection.output_format
            if output_format is None:
                output_resolver = getattr(profile, "effective_output_format", None)
                output_format = output_resolver() if callable(output_resolver) else getattr(profile, "output_format", None)
            return cmd, connection.prompt_as_arg, output_format
        cmd_resolver = getattr(profile, "effective_cmd", None)
        prompt_resolver = getattr(profile, "effective_prompt_as_arg", None)
        output_resolver = getattr(profile, "effective_output_format", None)
        if callable(cmd_resolver) and callable(prompt_resolver) and callable(output_resolver):
            return cmd_resolver(), prompt_resolver(), output_resolver()
        return (
            list(getattr(profile, "cmd", None) or []),
            bool(getattr(profile, "prompt_as_arg", False)),
            getattr(profile, "output_format", None),
        )

    @staticmethod
    def _should_use_warm_pool(profile, cmd: list[str], *, has_mcp_context: bool = False) -> bool:
        """Retorna se o profile permite processo pré-aquecido para execução CLI."""
        if not cmd:
            return False
        if has_mcp_context:
            return False
        profile_hook = getattr(type(profile), "should_use_warm_pool", None)
        if callable(profile_hook):
            return bool(profile_hook(profile, cmd))
        return bool(getattr(profile, "supports_warm_pool", True))

    @staticmethod
    def _profile_callable(profile, name: str):
        """Resolve callable real do profile sem aceitar atributos fabricados por mocks."""
        class_attr = getattr(type(profile), name, None)
        if callable(class_attr):
            return lambda *args, **kwargs: class_attr(profile, *args, **kwargs)
        profile_dict = getattr(profile, "__dict__", {})
        explicit_attr = profile_dict.get(name) if isinstance(profile_dict, dict) else None
        return explicit_attr if callable(explicit_attr) else None

    @staticmethod
    def _api_connection_signature(connection: OpenAIConnection) -> tuple:
        """Retorna assinatura estável da conexão usada para cache do driver API."""
        extra_body = getattr(connection, "extra_body", None)
        if isinstance(extra_body, dict):
            extra_body_sig = tuple(sorted(extra_body.items()))
        else:
            extra_body_sig = extra_body
        api_key_env = getattr(connection, "api_key_env", None)
        api_key_value = os.environ.get(api_key_env) if api_key_env else "ollama"
        api_key_fingerprint = (
            hashlib.sha256(api_key_value.encode("utf-8")).hexdigest()
            if api_key_value
            else None
        )
        return (
            getattr(connection, "model", None),
            getattr(connection, "base_url", None),
            getattr(connection, "api_key_env", None),
            getattr(connection, "provider", None),
            getattr(connection, "supports_native_tools", None),
            getattr(connection, "max_connections", None),
            getattr(connection, "max_model_requests", None),
            getattr(connection, "request_timeout", 300.0),
            api_key_fingerprint,
            extra_body_sig,
        )

    @staticmethod
    def _close_api_driver(driver) -> None:
        close = getattr(driver, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            _logger.exception("falha ao fechar driver OpenAI-compatible")

    def invalidate_api_driver(self, agent: str | None = None) -> None:
        """Invalida driver OpenAI-compatible cacheado para um agente ou todos."""
        if agent is None:
            drivers = list({id(driver): driver for driver in self._api_drivers.values()}.values())
            self._api_drivers.clear()
            self._api_driver_signatures.clear()
            for driver in drivers:
                self._close_api_driver(driver)
            return
        driver = self._api_drivers.pop(agent, None)
        self._api_driver_signatures.pop(agent, None)
        if driver is not None:
            self._close_api_driver(driver)

    # ------------------------------------------------------------------
    # call() — ponto de entrada principal
    # ------------------------------------------------------------------

    def call(
        self,
        agent,
        prompt: PromptText,
        silent=False,
        show_status=True,
        quiet=False,
        on_text_chunk=None,
        allow_tools=True,
        progress_callback=None,
        from_agent=None,
    ):
        """Executa um agente de forma serializada neste client.

        O cancelamento é limpo no início da rodada pelo chamador proprietário
        (por exemplo, ``ChatLifecycle``), nunca aqui. Assim um cancelamento que
        ocorre entre o precheck do gateway e esta chamada não é apagado.
        """
        with self._call_lock:
            if self._cancel_event.is_set():
                self._user_cancelled = True
                self._show_cancelled_once()
                return None
            return self._call_impl(
                agent,
                prompt,
                silent=silent,
                show_status=show_status,
                quiet=quiet,
                on_text_chunk=on_text_chunk,
                allow_tools=allow_tools,
                progress_callback=progress_callback,
                from_agent=from_agent,
            )

    def _call_impl(
        self,
        agent,
        prompt: PromptText,
        silent=False,
        show_status=True,
        quiet=False,
        on_text_chunk=None,
        allow_tools=True,
        progress_callback=None,
        from_agent=None,
    ):
        """Resolve o comando do agente e delega a execução."""
        profile = profiles.get(agent)
        if profile is None:
            self._show_error(f"[erro] agente desconhecido: {agent}")
            return None
        connection = self._resolve_profile_connection(profile)
        if isinstance(connection, OpenAIConnection):
            self._spy_output_presenter.set_turn_runtime("openai")
            return self._call_api(
                agent, profile, prompt,
                silent=silent,
                show_status=show_status,
                quiet=quiet,
                on_text_chunk=on_text_chunk,
                allow_tools=allow_tools,
                progress_callback=progress_callback,
                from_agent=from_agent,
        )
        self._spy_output_presenter.set_turn_runtime("cli")
        cmd, prompt_as_arg, output_format = self._resolve_profile_cli_attrs(profile, connection)
        extra_env = dict(connection.env or {}) if isinstance(connection, CliConnection) else {}
        env_hook = getattr(profile, "env_for_cli", None)
        if callable(env_hook):
            extra_env.update(env_hook())
        socket_descriptor = getattr(type(profile), "mcp_socket_path", None)
        if isinstance(socket_descriptor, property):
            socket_path = socket_descriptor.__get__(profile, type(profile))
        else:
            socket_path = getattr(profile, "_mcp_socket_path", None)
        has_mcp_context = (
            bool(isinstance(socket_path, str) and socket_path.strip())
            or "OPENCODE_CONFIG_CONTENT" in extra_env
            or "QUIMERA_FAKE_MCP_SOCKET" in extra_env
        )
        if agent and has_mcp_context:
            extra_env["QUIMERA_MCP_AGENT_NAME"] = str(agent)
            if from_agent:
                extra_env["QUIMERA_MCP_PARENT_AGENT"] = str(from_agent)
        tool_config = getattr(self.tool_executor, "config", None)
        if getattr(tool_config, "allow_ask_user", True) is False:
            current = str(extra_env.get("QUIMERA_MCP_DISABLED_TOOLS") or "")
            disabled_tools = [name.strip() for name in current.split(",") if name.strip()]
            if "ask_user" not in disabled_tools:
                disabled_tools.append("ask_user")
            extra_env["QUIMERA_MCP_DISABLED_TOOLS"] = ",".join(disabled_tools)
        if self.tool_executor is not None:
            get_approval_scope = getattr(self.tool_executor, "get_thread_approval_scope", None)
            if callable(get_approval_scope):
                approval_scope = get_approval_scope()
                if approval_scope:
                    extra_env["QUIMERA_MCP_APPROVAL_SCOPE"] = approval_scope
        extra_env = extra_env or None
        cwd = connection.cwd if isinstance(connection, CliConnection) else None
        run_kwargs = {
            "silent": silent,
            "agent": agent,
            "show_status": show_status,
            "progress_callback": progress_callback,
        }
        if extra_env is not None:
            run_kwargs["extra_env"] = extra_env
        if cwd is not None:
            run_kwargs["cwd"] = cwd
        if prompt_as_arg:
            raw = self.run([*cmd, prompt], input_text=None, **run_kwargs)
        else:
            _extra_env = run_kwargs.get("extra_env")
            _effective_cmd, _effective_cwd = self._build_effective_cmd(cmd, agent, run_kwargs.get("cwd"))
            _use_warm_pool = self._should_use_warm_pool(
                profile,
                cmd,
                has_mcp_context=has_mcp_context,
            )
            _slot = self._warm_pool.take(_effective_cmd, _effective_cwd, _extra_env) if _use_warm_pool else None
            if not _use_warm_pool and not has_mcp_context:
                # Se houver um slot antigo para esse comando, descarta para evitar
                # processos ociosos extras no gerenciador.
                _stale_slot = self._warm_pool.take(_effective_cmd, _effective_cwd, _extra_env)
                if _stale_slot is not None:
                    _stale_slot.discard()
            format_stdin_input = self._profile_callable(profile, "format_stdin_input")
            stdin_input = format_stdin_input(prompt) if callable(format_stdin_input) else prompt
            raw = self.run(cmd, input_text=stdin_input, _primed_proc=_slot.proc if _slot else None, **run_kwargs)
            if _use_warm_pool:
                self._warm_pool.schedule_warm(
                    _effective_cmd,
                    self._build_run_env(_extra_env),
                    _effective_cwd,
                    _extra_env,
                )
        fmt = output_format
        if fmt == "stream-json" and raw is not None:
            return parse_stream_json(raw, agent, self.tool_event_callback)
        if fmt == "codex-json" and raw is not None:
            return parse_codex_json(raw, agent, self.tool_event_callback)
        if fmt == "opencode-json" and raw is not None:
            return parse_opencode_json(raw, agent, self.tool_event_callback)
        return raw

    def _call_api(
        self,
        agent,
        profile,
        prompt: PromptText,
        silent=False,
        show_status=True,
        quiet=False,
        on_text_chunk=None,
        allow_tools=True,
        progress_callback=None,
        from_agent=None,
    ):
        """Executa agentes com driver de API (ex: openai_compat para Ollama)."""
        connection = self._resolve_profile_connection(profile)
        if not isinstance(connection, OpenAIConnection):
            self._show_error(f"[erro] conexão inválida para driver de API: {agent}")
            return None
        signature = self._api_connection_signature(connection)
        cached_signature = self._api_driver_signatures.get(agent)
        if cached_signature is not None and cached_signature != signature:
            self.invalidate_api_driver(agent)
        is_first_call = agent not in self._api_drivers
        if is_first_call:
            global OpenAICompatDriver
            if OpenAICompatDriver is None:
                from quimera.runtime.drivers.openai_compat import OpenAICompatDriver as _OpenAICompatDriver

                OpenAICompatDriver = _OpenAICompatDriver

            api_key_env = connection.api_key_env
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if not api_key:
                    self._show_error(
                        f"[erro] variável de ambiente '{api_key_env}' não definida para {agent}"
                    )
                    return None
            else:
                api_key = "ollama"
            self._api_drivers[agent] = OpenAICompatDriver(
                model=connection.model,
                base_url=connection.base_url,
                api_key=api_key,
                timeout=getattr(connection, "request_timeout", 300.0),
                tool_use_reliability=getattr(profile, "tool_use_reliability", "medium"),
                extra_body=connection.extra_body,
                max_connections=getattr(connection, "max_connections", 4),
                max_model_requests=getattr(connection, "max_model_requests", None),
            )
            self._api_driver_signatures[agent] = signature

        driver_instance = self._api_drivers[agent]
        if self._cancel_event.is_set():
            self._user_cancelled = True
            self._show_cancelled_once()
            return None
        self.rate_limit_detected = False
        self.rate_limit_detected_at = None
        self.rate_limit_retry_after = None
        self._agent_running = True
        self._running_agent = agent
        self._start_esc_monitor()
        status_cm = self.renderer.running_status("", agent=agent) if (
                show_status and not silent and not quiet) else nullcontext(None)
        status_label = f"[dim]{'conectando' if is_first_call else 'aguardando'} {connection.model}...[/dim]"
        effective_tool_executor = None

        try:
            with status_cm as status:
                if status is not None:
                    status.update(status_label)
                if (
                    allow_tools
                    and getattr(profile, "supports_tools", True)
                    and getattr(connection, "supports_native_tools", True)
                ):
                    effective_tool_executor = self.tool_executor
                approval_scope = None
                if effective_tool_executor is not None:
                    get_approval_scope = getattr(effective_tool_executor, "get_thread_approval_scope", None)
                    if callable(get_approval_scope):
                        approval_scope = get_approval_scope()
                if effective_tool_executor is not None:
                    # O driver sempre inclui o agente em trusted_context. Manter
                    # o callback baseado em metadata evita fixar o executor no
                    # último agente OpenAI e contaminar chamadas MCP posteriores.
                    self.bind_tool_preview_callback(effective_tool_executor, agent=agent)
                # Injeta callbacks de spinner no executor para que o approval handler
                # possa pausar o Live do Rich antes de input() bloqueante, evitando
                # race condition entre o refresh do spinner e a leitura do stdin.
                if effective_tool_executor is not None and status is not None:
                    _live = getattr(status, '_live', None)
                    if _live is not None:
                        effective_tool_executor.set_spinner_callbacks(
                            suspend_spinner_fn=lambda: _live.stop(),
                            resume_spinner_fn=lambda: _live.start(),
                        )
                result_holder = {"result": None, "error": None}
                api_run_token = object()
                api_cancel_event = threading.Event()
                if self._cancel_event.is_set():
                    api_cancel_event.set()

                def _run_driver():
                    previous_scope = None
                    previous_cancel_event = None
                    try:
                        if effective_tool_executor is not None:
                            bind_approval_scope = getattr(
                                effective_tool_executor,
                                "bind_thread_approval_scope",
                                None,
                            )
                            if callable(bind_approval_scope):
                                previous_scope = bind_approval_scope(approval_scope)
                            # Vincula o cancel_event à thread do driver com
                            # restauração segura, em vez de gravar num slot global
                            # do handler compartilhado (evita corrida entre
                            # clients concorrentes usando o mesmo executor).
                            bind_cancel_event = getattr(
                                effective_tool_executor,
                                "bind_approval_cancel_event",
                                None,
                            )
                            if callable(bind_cancel_event):
                                previous_cancel_event = bind_cancel_event(api_cancel_event)

                        guarded_on_text_chunk = None
                        if on_text_chunk is not None:
                            def guarded_on_text_chunk(chunk):
                                if not api_cancel_event.is_set():
                                    on_text_chunk(chunk)

                        result_holder["result"] = driver_instance.run(
                            prompt=prompt,
                            tool_executor=effective_tool_executor,
                            agent_name=agent,
                            parent_agent=from_agent,
                            session_id=self.session_id,
                            base_dir=self.workspace_tmp_root,
                            quiet=quiet,
                            cancel_event=api_cancel_event,
                            on_tool_result=(lambda tool_result: self.tool_event_callback(agent, result=tool_result))
                            if self.tool_event_callback else None,
                            on_tool_abort=(
                                lambda reason: self.tool_event_callback(agent, loop_abort=True, reason=reason))
                            if self.tool_event_callback else None,
                            on_text_chunk=guarded_on_text_chunk,
                            progress_callback=progress_callback,
                        )
                    except Exception as exc:
                        result_holder["error"] = exc
                    finally:
                        if effective_tool_executor is not None:
                            bind_approval_scope = getattr(
                                effective_tool_executor,
                                "bind_thread_approval_scope",
                                None,
                            )
                            if callable(bind_approval_scope):
                                bind_approval_scope(previous_scope)
                            bind_cancel_event = getattr(
                                effective_tool_executor,
                                "bind_approval_cancel_event",
                                None,
                            )
                            if callable(bind_cancel_event):
                                bind_cancel_event(previous_cancel_event)
                        self._unregister_api_run(api_run_token)

                t = threading.Thread(target=_run_driver, daemon=True)
                self._register_api_run(api_run_token, api_cancel_event)
                t.start()

                _api_start = time.time()
                cancellation_announced = False
                timed_out = False
                while t.is_alive():
                    if effective_tool_executor is not None:
                        process_input = getattr(
                            effective_tool_executor,
                            "process_pending_input_once",
                            None,
                        )
                        if callable(process_input) and process_input():
                            continue
                    if self._cancel_event.is_set() and not api_cancel_event.is_set():
                        api_cancel_event.set()
                    if api_cancel_event.is_set():
                        if not timed_out and not cancellation_announced:
                            self._show_cancelled_once()
                            cancellation_announced = True
                        # A chamada HTTP do SDK pode permanecer bloqueada até o
                        # timeout do transporte. Não abandonamos a thread: enquanto
                        # ela estiver viva o scheduler deve continuar contabilizando
                        # a execução, impedindo que um segundo Ctrl+C seja tratado
                        # como saída do app. O driver recebe o mesmo cancel_event e
                        # descarta qualquer resposta tardia antes de executar tools.
                        t.join(timeout=0.1)
                        continue
                    time.sleep(0.25)
                    _api_elapsed = time.time() - _api_start
                    if progress_callback:
                        progress_callback(f"aguardando resposta da API ({connection.model})... {int(_api_elapsed)}s")

                    if _api_elapsed > MAX_WALL_CLOCK_SECONDS and not timed_out:
                        timed_out = True
                        api_cancel_event.set()
                        self._show_error(
                            f"[erro] wall-clock timeout after {MAX_WALL_CLOCK_SECONDS}s em driver API")
                        # Não libere o client enquanto o driver ainda usa executor,
                        # approval callbacks e semáforo do backend. O timeout do
                        # transporte limita a espera pelo encerramento cooperativo.
                        continue

                if timed_out:
                    return None

                if api_cancel_event.is_set():
                    # O próprio driver pode concluir imediatamente após
                    # sinalizar o evento, sem permanecer vivo tempo suficiente
                    # para o loop acima observar o cancelamento. Normalize o
                    # estado público do client também nesse caminho rápido.
                    self._user_cancelled = True
                    self._show_cancelled_once()
                    return None

                if result_holder["error"]:
                    error = result_holder["error"]
                    if isinstance(error, _TransientAPIError):
                        if error.rate_limited:
                            self.rate_limit_detected = True
                            self.rate_limit_retry_after = error.retry_after
                            if self.rate_limit_detected_at is None:
                                self.rate_limit_detected_at = time.time()
                        _cmd = getattr(profile, "cmd", None)
                        _name = (
                            (_cmd[0] if isinstance(_cmd, (list, tuple)) and _cmd else None)
                            or connection.model or "driver"
                        )
                        self._show_error(f"[erro] falha ao comunicar com {_name}: {error}")
                        return None
                    if isinstance(error, (_FatalAPIError, _APIExecutionError)):
                        # A camada de dispatch é a única responsável por exibir
                        # erros não-retryable, evitando mensagens duplicadas.
                        raise error
                    if _is_rate_limit_signal(str(error)):
                        self.rate_limit_detected = True
                        if self.rate_limit_detected_at is None:
                            self.rate_limit_detected_at = time.time()
                    _cmd = getattr(profile, "cmd", None)
                    _name = (
                        (_cmd[0] if isinstance(_cmd, (list, tuple)) and _cmd else None)
                        or connection.model or "driver"
                    )
                    self._show_error(f"[erro] falha ao comunicar com {_name}: {error}")
                    return None

                return result_holder["result"]
        finally:
            if effective_tool_executor is not None:
                # Limpa callbacks de spinner para não manter referência a Live encerrado
                clear_spinner = getattr(effective_tool_executor, "set_spinner_callbacks", None)
                if callable(clear_spinner):
                    clear_spinner(None, None)
            self._agent_running = False
            self._running_agent = None
            self._stop_esc_monitor()

    def flush_pending_summary(self) -> None:
        """Renderiza o resumo de turno pendente; deve ser chamado após fechar o stream."""
        pending = self._pending_summary_render
        self._pending_summary_render = None
        if pending is None:
            return
        agent, detail, should_render = pending
        if should_render:
            self._spy_output_presenter._render_turn_summary(agent, detail)

    def close(self) -> None:
        """Encerra o cliente, liberando processos pré-aquecidos pendentes."""
        self.invalidate_api_driver()
        self._warm_pool.shutdown()

    # ------------------------------------------------------------------
    # Métricas
    # ------------------------------------------------------------------

    def log_prompt_metrics(
            self, agent, metrics, session_id=None,
            round_index=0, session_call_index=0,
            history_window=12, protocol_mode="standard",
    ):
        """Persiste métricas do prompt em JSONL quando metrics_file estiver configurado."""
        largest_block = max(
            (
                ("rules", metrics.get("rules_chars", 0)),
                ("session_state", metrics.get("session_state_chars", 0)),
                ("persistent", metrics.get("persistent_chars", 0)),
                ("history", metrics.get("history_chars", 0)),
                ("delegation", metrics.get("delegation_chars", 0)),
            ),
            key=lambda item: item[1],
        )
        if self.metrics_file:
            record = {
                "session_id": session_id,
                "round_index": round_index,
                "session_call_index": session_call_index,
                "agent": agent,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "history_window": history_window,
                "protocol_mode": protocol_mode,
                "largest_block": largest_block[0],
                **metrics,
            }
            with self._metrics_lock:
                with open(self.metrics_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
