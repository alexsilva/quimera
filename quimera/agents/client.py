"""AgentClient: orquestra chamadas a agentes externos (CLI e API)."""
import json
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
from quimera.runtime.drivers.openai_compat import OpenAICompatDriver
from quimera.runtime.tool_preview import ToolPreview
from quimera.prompt_templates import PromptText

from quimera.agents.parsers import parse_stream_json, parse_codex_json, parse_opencode_json
from quimera.agents.process_runner import ProcessRunner, MAX_WALL_CLOCK_SECONDS
from quimera.agents.signal_guard import EscMonitor, terminate_process_group
from quimera.agents.warm_pool import WarmPool
from quimera.runtime.process_supervisor import ProcessSupervisor
from quimera.agents.text_filters import (
    _strip_spinner,
    _should_ignore_stderr_line,
    _filter_stderr_lines,
    _is_rate_limit_signal,
)

_logger = logging.getLogger(__name__)


class _FrozenSession:
    """Rastreia session_id para agente fixado com suporte a resume."""

    def __init__(self, agent: str):
        self.agent = agent
        self.session_id: str | None = None


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
        self.tool_event_callback = None
        # Modo de execução ativo; quando definido, subprocessos são envolvidos com bwrap.
        self.execution_mode = None
        self._cancel_event = threading.Event()
        self._esc_monitor = EscMonitor(self._cancel_event)
        self._user_cancelled = False
        self._cancel_notice_shown = False
        self._cancel_notice_lock = threading.Lock()
        self._agent_running = False
        self._current_proc = None
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
        self._warm_pool = WarmPool()
        self.process_supervisor: ProcessSupervisor | None = process_supervisor
        self._persistent_sessions: dict[str, _FrozenSession] = {}

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
        show_system_neutral = getattr(self.renderer, "show_system_neutral", None)
        if callable(show_system_neutral):
            show_system_neutral(message)
            return
        self.renderer.show_system(message)

    def _show_tool_preview(self, message: str, *, agent: str | None = None) -> None:
        """Exibe preview operacional de tool no feed quando possível."""
        if getattr(self.renderer, "supports_agent_feed", False) is True:
            show_feed = getattr(self.renderer, "show_feed", None)
            if callable(show_feed):
                show_feed(message, agent=agent, muted=True)
                return
        self._show_muted(message)

    def bind_tool_preview_callback(self, tool_executor, *, agent: str | None = None) -> None:
        """Registra o preview operacional compartilhado para tools sem approval."""
        set_tool_preview = getattr(tool_executor, "set_tool_preview_callback", None)
        if callable(set_tool_preview):
            set_tool_preview(
                lambda name, args, metadata=None: self._show_tool_preview(
                    ToolPreview.build(name, args),
                    agent=agent or self._agent_from_tool_metadata(metadata),
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
        return None

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
            self._cancel_notice_shown = False

    def reset_cancel_state(self) -> None:
        """Limpa estado de cancelamento antes de uma nova rodada."""
        self._user_cancelled = False
        self._cancel_event.clear()
        self.reset_cancel_notices()

    def _show_cancelled_once(self) -> None:
        """Evita repetição de '[cancelado] pelo usuário' em cancelamentos concorrentes."""
        should_show = False
        with self._cancel_notice_lock:
            if not self._cancel_notice_shown:
                self._cancel_notice_shown = True
                should_show = True
        if should_show:
            ts = datetime.now().strftime("%H:%M:%S")
            self._show_error(f"[cancelado] pelo usuário às {ts}")

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

    def cancel_active_work(self) -> None:
        """Cancela o trabalho atual e encerra subprocessos ainda vivos."""
        self._user_cancelled = True
        self._cancel_event.set()
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
        if agent and hasattr(self.renderer, "update_agent_transient"):
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
        self._cancel_event.clear()
        self.rate_limit_detected = False
        self.rate_limit_detected_at = None
        self._agent_running = True
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
            self._stop_esc_monitor()
            self._show_error(f"[erro] falha ao enviar input para {cmd[0]}: {exc}")
            proc.kill()
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
                with nullcontext(None) as status:
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
            self._stop_esc_monitor()
            self._spy_output_presenter.reset()

        if proc.returncode != 0:
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
                api_key_env=getattr(profile, "api_key_env", None) or "OPENAI_API_KEY",
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
    def _should_use_warm_pool(profile, cmd: list[str]) -> bool:
        """Retorna se o profile permite processo pré-aquecido para execução CLI."""
        if not cmd:
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

    def open_persistent_session(self, agent: str) -> bool:
        """Ativa rastreamento de sessão para agente fixado, quando o perfil suporta."""
        profile = profiles.get(agent)
        if profile is None or not getattr(profile, "supports_resume", False):
            return False
        if agent not in self._persistent_sessions:
            self._persistent_sessions[agent] = _FrozenSession(agent)
        return True

    def close_persistent_session(self, agent: str) -> None:
        """Remove rastreamento de sessão do agente ao descongelar."""
        self._persistent_sessions.pop(agent, None)

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
    ):
        """Resolve o comando do agente e delega a execução."""
        self._user_cancelled = False
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
        )
        self._spy_output_presenter.set_turn_runtime("cli")
        frozen_session = self._persistent_sessions.get(agent)
        cmd, prompt_as_arg, output_format = self._resolve_profile_cli_attrs(profile, connection)
        if frozen_session is not None and frozen_session.session_id:
            inject_resume_arg = self._profile_callable(profile, "inject_resume_arg")
            if callable(inject_resume_arg):
                cmd = inject_resume_arg(cmd, frozen_session.session_id)
        extra_env = dict(connection.env or {}) if isinstance(connection, CliConnection) else {}
        env_hook = getattr(profile, "env_for_cli", None)
        if callable(env_hook):
            extra_env.update(env_hook())
        socket_path = getattr(profile, "_mcp_socket_path", None)
        has_mcp_context = (
            bool(isinstance(socket_path, str) and socket_path.strip())
            or "OPENCODE_CONFIG_CONTENT" in extra_env
            or "QUIMERA_FAKE_MCP_SOCKET" in extra_env
        )
        if agent and has_mcp_context:
            extra_env["QUIMERA_MCP_AGENT_NAME"] = str(agent)
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
            _has_active_session_id = frozen_session is not None and bool(frozen_session.session_id)
            _use_warm_pool = not _has_active_session_id and self._should_use_warm_pool(profile, cmd)
            _slot = self._warm_pool.take(_effective_cmd, _effective_cwd, _extra_env) if _use_warm_pool else None
            if not _use_warm_pool:
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
        if frozen_session is not None and raw:
            extract_session_id = self._profile_callable(profile, "extract_session_id")
            new_session_id = extract_session_id(raw) if callable(extract_session_id) else None
            if new_session_id:
                frozen_session.session_id = new_session_id
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
    ):
        """Executa agentes com driver de API (ex: openai_compat para Ollama)."""
        connection = self._resolve_profile_connection(profile)
        if not isinstance(connection, OpenAIConnection):
            self._show_error(f"[erro] conexão inválida para driver de API: {agent}")
            return None
        is_first_call = agent not in self._api_drivers
        if is_first_call:
            api_key_env = connection.api_key_env
            api_key = os.environ.get(api_key_env, "ollama") if api_key_env else "ollama"
            self._api_drivers[agent] = OpenAICompatDriver(
                model=connection.model,
                base_url=connection.base_url,
                api_key=api_key,
                timeout=self.idle_timeout,
                tool_use_reliability=getattr(profile, "tool_use_reliability", "medium"),
                extra_body=connection.extra_body,
                max_connections=getattr(connection, "max_connections", 4),
            )

        driver_instance = self._api_drivers[agent]
        self._cancel_event.clear()
        self.rate_limit_detected = False
        self.rate_limit_detected_at = None
        self._agent_running = True
        self._start_esc_monitor()
        status_cm = self.renderer.running_status("", agent=agent) if (
                show_status and not silent and not quiet) else nullcontext(None)
        status_label = f"[dim]{'conectando' if is_first_call else 'aguardando'} {connection.model}...[/dim]"
        effective_tool_executor = None

        try:
            with status_cm as status:
                if status is not None:
                    status.update(status_label)
                if allow_tools and getattr(profile, "supports_tools", True):
                    effective_tool_executor = self.tool_executor
                if effective_tool_executor is not None:
                    set_cancel_event = getattr(effective_tool_executor, "set_approval_cancel_event", None)
                    if callable(set_cancel_event):
                        set_cancel_event(self._cancel_event)
                approval_scope = None
                if effective_tool_executor is not None:
                    get_approval_scope = getattr(effective_tool_executor, "get_thread_approval_scope", None)
                    if callable(get_approval_scope):
                        approval_scope = get_approval_scope()
                if effective_tool_executor is not None:
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

                def _run_driver():
                    previous_scope = None
                    try:
                        if effective_tool_executor is not None:
                            bind_approval_scope = getattr(
                                effective_tool_executor,
                                "bind_thread_approval_scope",
                                None,
                            )
                            if callable(bind_approval_scope):
                                previous_scope = bind_approval_scope(approval_scope)

                        result_holder["result"] = driver_instance.run(
                            prompt=prompt,
                            tool_executor=effective_tool_executor,
                            agent_name=agent,
                            session_id=self.session_id,
                            base_dir=self.workspace_tmp_root,
                            quiet=quiet,
                            cancel_event=self._cancel_event,
                            on_tool_result=(lambda tool_result: self.tool_event_callback(agent, result=tool_result))
                            if self.tool_event_callback else None,
                            on_tool_abort=(
                                lambda reason: self.tool_event_callback(agent, loop_abort=True, reason=reason))
                            if self.tool_event_callback else None,
                            on_text_chunk=on_text_chunk,
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

                t = threading.Thread(target=_run_driver, daemon=True)
                t.start()

                _api_start = time.time()
                while t.is_alive():
                    if effective_tool_executor is not None:
                        process_input = getattr(
                            effective_tool_executor,
                            "process_pending_input_once",
                            None,
                        )
                        if callable(process_input) and process_input():
                            continue
                    if self._cancel_event.is_set():
                        self.cancel_active_work()
                        t.join(timeout=0.5)
                        self._show_cancelled_once()
                        return None
                    time.sleep(0.25)
                    _api_elapsed = time.time() - _api_start
                    if progress_callback:
                        progress_callback(f"aguardando resposta da API ({connection.model})... {int(_api_elapsed)}s")

                    if _api_elapsed > MAX_WALL_CLOCK_SECONDS:
                        self._cancel_event.set()
                        self._show_error(
                            f"[erro] wall-clock timeout after {MAX_WALL_CLOCK_SECONDS}s em driver API")
                        return None

                if self._cancel_event.is_set() and result_holder["result"] is None:
                    self.cancel_active_work()
                    return None

                if result_holder["error"]:
                    if _is_rate_limit_signal(str(result_holder["error"])):
                        self.rate_limit_detected = True
                        if self.rate_limit_detected_at is None:
                            self.rate_limit_detected_at = time.time()
                    _cmd = getattr(profile, "cmd", None)
                    _name = (
                        (_cmd[0] if isinstance(_cmd, (list, tuple)) and _cmd else None)
                        or connection.model or "driver"
                    )
                    self._show_error(f"[erro] falha ao comunicar com {_name}: {result_holder['error']}")
                    return None

                return result_holder["result"]
        finally:
            if effective_tool_executor is not None:
                set_cancel_event = getattr(effective_tool_executor, "set_approval_cancel_event", None)
                if callable(set_cancel_event):
                    set_cancel_event(None)
                # Limpa callbacks de spinner para não manter referência a Live encerrado
                clear_spinner = getattr(effective_tool_executor, "set_spinner_callbacks", None)
                if callable(clear_spinner):
                    clear_spinner(None, None)
            self._agent_running = False
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
        self._warm_pool.shutdown()
        self._persistent_sessions.clear()

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
