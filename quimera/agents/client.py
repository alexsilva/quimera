"""AgentClient: orquestra chamadas a agentes externos (CLI e API)."""
import json
import io
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone

import quimera.plugins as plugins
from quimera.constants import MAX_STDERR_LINES, Visibility
from quimera.plugins.base import CliConnection, OpenAIConnection
from quimera.sandbox.bwrap import build_bwrap_cmd
from quimera.spy_output_presenter import SpyOutputPresenter
from quimera.runtime.drivers.openai_compat import OpenAICompatDriver

from quimera.agents.parsers import parse_stream_json, parse_codex_json, parse_opencode_json
from quimera.agents.process_runner import ProcessRunner
from quimera.agents.signal_guard import EscMonitor, terminate_process_group
from quimera.agents.text_filters import (
    _strip_spinner,
    _should_ignore_stderr_line,
    _filter_stderr_lines,
    _is_rate_limit_signal,
)

_logger = logging.getLogger(__name__)

_GUI_VARS = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SYSTEM_BUS_ADDRESS", "XAUTHORITY", "XDG_RUNTIME_DIR",
})


class AgentClient:
    """Executa os agentes externos no diretório de trabalho do projeto."""

    def __init__(self, renderer, metrics_file=None, timeout=None, visibility=Visibility.SUMMARY,
                 working_dir=None, workspace_root=None, tool_executor=None):
        """Inicializa uma instância de AgentClient."""
        self.renderer = renderer
        self.metrics_file = metrics_file
        self._metrics_lock = threading.Lock()
        self.timeout = timeout
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
        self._agent_running = False
        self._current_proc = None
        self._spy_output_presenter = SpyOutputPresenter(self.renderer, self.visibility)
        self.rate_limit_detected = False
        self.rate_limit_detected_at: float | None = None

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

    # ------------------------------------------------------------------
    # Formatação de stdout ao vivo
    # ------------------------------------------------------------------

    def _show_formatted_stdout(self, agent: str | None, line: str) -> bool:
        """Exibe mensagens resumidas de stdout quando o plugin oferece formatter."""
        return self._spy_output_presenter.consume_stdout(agent, line)

    # ------------------------------------------------------------------
    # run() — execução de subprocess
    # ------------------------------------------------------------------

    def run(self, cmd, input_text=None, silent=False, agent=None, show_status=True, extra_env=None, cwd=None):
        """Executa run."""
        self._cancel_event.clear()
        self.rate_limit_detected = False
        self.rate_limit_detected_at = None
        self._agent_running = True
        self._start_esc_monitor()
        try:
            env = {k: v for k, v in os.environ.items() if k not in _GUI_VARS}
            env.update({"NO_COLOR": "1", "TERM": "dumb", "COLORTERM": ""})
            if extra_env:
                env.update(extra_env)
            effective_cmd = cmd
            effective_cwd = cwd or self.working_dir
            if self.execution_mode is not None and effective_cwd:
                effective_cmd = build_bwrap_cmd(
                    self.execution_mode,
                    effective_cwd,
                    cmd,
                    plugin=plugins.get(agent) if agent else None,
                )
            proc = subprocess.Popen(
                effective_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=effective_cwd,
                start_new_session=True,
            )
            self._current_proc = proc
        except OSError as exc:
            self._agent_running = False
            self._stop_esc_monitor()
            self.renderer.show_error(f"[erro] não foi possível iniciar {cmd[0]}: {exc}")
            return None

        result_holder = {  # io.StringIO evita lista crescente; deque limita stderr
            "stdout": io.StringIO(),
            "stderr": deque(maxlen=MAX_STDERR_LINES * 2),
            "error": None,
        }
        log_queue = queue.Queue() if not silent else None
        stderr_lines_shown = 0
        self._spy_output_presenter.reset()

        def _read_stdout():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        result_holder["stdout"].write(line)
                        if log_queue is not None and self.visibility in {Visibility.SUMMARY, Visibility.FULL}:
                            log_queue.put(("stdout", line))
            except Exception as exc:
                result_holder["error"] = exc

        def _read_stderr():
            try:
                if proc.stderr:
                    for line in proc.stderr:
                        result_holder["stderr"].append(line)
                        if log_queue is not None:
                            log_queue.put(("stderr", line))
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
            self.renderer.show_error(f"[erro] falha ao enviar input para {cmd[0]}: {exc}")
            proc.kill()
            return None

        runner = ProcessRunner(
            proc, stdout_thread, stderr_thread, result_holder,
            self._cancel_event, self.timeout,
        )

        try:
            if silent:
                termination = runner.watch()
                self.rate_limit_detected = runner.rate_limit_detected
                self.rate_limit_detected_at = runner.rate_limit_detected_at

                if termination == ProcessRunner.CANCELLED:
                    self._user_cancelled = True
                    self._agent_running = False
                    self._current_proc = None
                    self._stop_esc_monitor()
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
                    _logger.warning("[erro] wall-clock timeout after %ds for %s", self.timeout * 5, cmd[0])
                    return None

                if result_holder["stdout"]:
                    _logger.debug(result_holder["stdout"].getvalue())
                filtered_stderr = _filter_stderr_lines(agent, list(result_holder["stderr"]))
                if filtered_stderr:
                    _logger.warning("".join(filtered_stderr))

            else:
                assert log_queue is not None
                status_cm = self.renderer.running_status("", agent=agent) if show_status else nullcontext(None)

                if self.visibility == Visibility.SUMMARY:
                    self.renderer.show_plain(f"→ {cmd[0]} iniciando...", agent=agent)

                with status_cm as status:
                    def _on_item(stream_type, line):
                        nonlocal stderr_lines_shown
                        if stream_type == "stderr" and _is_rate_limit_signal(line):
                            runner.notify_rate_limit()
                        if status is not None:
                            _lbl = self._spy_output_presenter.compose_status_label(cmd[0])
                            status.update(f"[dim]{_lbl}... {_elapsed[0]}s[/dim]")
                        cleaned = _strip_spinner(line.rstrip("\n"))
                        if not cleaned.strip():
                            return
                        if stream_type == "stdout":
                            if self.visibility in {Visibility.SUMMARY, Visibility.FULL}:
                                self._show_formatted_stdout(agent, cleaned)
                            return
                        self._spy_output_presenter.flush(agent)
                        if stream_type == "stderr" and _should_ignore_stderr_line(agent, line):
                            return
                        if stream_type == "stderr" and self.visibility != Visibility.FULL:
                            if stderr_lines_shown < MAX_STDERR_LINES:
                                self.renderer.show_plain(cleaned, agent=agent)
                                stderr_lines_shown += 1
                            elif stderr_lines_shown == MAX_STDERR_LINES:
                                self.renderer.show_plain(
                                    f"... (stderr truncado, máximo {MAX_STDERR_LINES} linhas)", agent=agent)
                                stderr_lines_shown += 1
                        else:
                            self.renderer.show_plain(cleaned, agent=agent)

                    _elapsed = [0]  # mutable cell for on_item closure

                    def _on_tick(elapsed):
                        _elapsed[0] = elapsed
                        if status is not None:
                            _lbl = self._spy_output_presenter.compose_status_label(cmd[0])
                            status.update(f"[dim]{_lbl}... {elapsed}s[/dim]")

                    termination = runner.watch(log_queue=log_queue, on_item=_on_item, on_tick=_on_tick)
                    self.rate_limit_detected = runner.rate_limit_detected
                    self.rate_limit_detected_at = runner.rate_limit_detected_at

                    if termination == ProcessRunner.CANCELLED:
                        self._user_cancelled = True
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        self.renderer.show_error("[cancelado] pelo usuário")
                        return None
                    if termination == ProcessRunner.RATE_LIMIT:
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        self.renderer.show_error(
                            f"[rate limit] {cmd[0]} em espera; cedendo para outros agentes")
                        return None
                    if termination == ProcessRunner.TIMEOUT:
                        self._agent_running = False
                        self._current_proc = None
                        self._stop_esc_monitor()
                        wall_limit = self.timeout * 5
                        self.renderer.show_error(
                            f"[erro] wall-clock timeout after {wall_limit}s for {cmd[0]}")
                        return None

            self._spy_output_presenter.flush(agent)
            proc.wait()
            if not silent and self.visibility == Visibility.SUMMARY:
                status_word = "concluído" if proc.returncode == 0 else f"falhou (código {proc.returncode})"
                self.renderer.show_plain(f"← {cmd[0]} {status_word}", agent=agent)
            self._agent_running = False
            self._current_proc = None
            self._stop_esc_monitor()

            if result_holder["error"]:
                self.renderer.show_error(f"[erro] falha ao comunicar com {cmd[0]}: {result_holder['error']}")
                return None

            output = result_holder["stdout"].getvalue().strip()
            error = "".join(_filter_stderr_lines(agent, list(result_holder["stderr"]))).strip()
        finally:
            self._agent_running = False
            self._stop_esc_monitor()
            self._spy_output_presenter.reset()

        if proc.returncode != 0:
            self.renderer.show_error(f"[erro] agente {cmd[0]} retornou código {proc.returncode}")
            if error and stderr_lines_shown <= MAX_STDERR_LINES:
                tail = "\n".join(error.splitlines()[-5:])
                self.renderer.show_error(tail)
            return None

        if not output:
            if error:
                self.renderer.show_error(f"[erro] agente {cmd[0]} não retornou saída válida")
                if stderr_lines_shown <= MAX_STDERR_LINES:
                    tail = "\n".join(error.splitlines()[-5:])
                    self.renderer.show_error(tail)
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
    # Plugin resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_plugin_connection(plugin):
        """Resolve a conexão efetiva com fallback para objetos plugin simplificados."""
        resolver = getattr(plugin, "effective_connection", None)
        if callable(resolver):
            connection = resolver()
            if isinstance(connection, (OpenAIConnection, CliConnection)):
                return connection
        driver = getattr(plugin, "driver", "cli")
        if isinstance(driver, str) and driver != "cli":
            return OpenAIConnection(
                model=getattr(plugin, "model", None) or "gpt-4o",
                base_url=getattr(plugin, "base_url", None) or "https://api.openai.com/v1",
                api_key_env=getattr(plugin, "api_key_env", None) or "OPENAI_API_KEY",
                provider=driver,
                supports_native_tools=getattr(plugin, "supports_tools", True),
            )
        return CliConnection(
            cmd=list(getattr(plugin, "cmd", None) or []),
            prompt_as_arg=getattr(plugin, "prompt_as_arg", False),
            output_format=getattr(plugin, "output_format", None),
        )

    @staticmethod
    def _resolve_plugin_cli_attrs(plugin, connection) -> tuple[list[str], bool, str | None]:
        """Resolve atributos CLI com fallback para plugins simplificados em testes."""
        if isinstance(connection, CliConnection):
            return list(connection.cmd), connection.prompt_as_arg, connection.output_format
        cmd_resolver = getattr(plugin, "effective_cmd", None)
        prompt_resolver = getattr(plugin, "effective_prompt_as_arg", None)
        output_resolver = getattr(plugin, "effective_output_format", None)
        if callable(cmd_resolver) and callable(prompt_resolver) and callable(output_resolver):
            return cmd_resolver(), prompt_resolver(), output_resolver()
        return (
            list(getattr(plugin, "cmd", None) or []),
            bool(getattr(plugin, "prompt_as_arg", False)),
            getattr(plugin, "output_format", None),
        )

    # ------------------------------------------------------------------
    # call() — ponto de entrada principal
    # ------------------------------------------------------------------

    def call(
        self,
        agent,
        prompt,
        silent=False,
        show_status=True,
        quiet=False,
        on_text_chunk=None,
        allow_tools=True,
    ):
        """Resolve o comando do agente e delega a execução."""
        self._user_cancelled = False
        if self.execution_mode and self.execution_mode.prompt_addon:
            prompt = f"{self.execution_mode.prompt_addon}\n\n{prompt}"
        plugin = plugins.get(agent)
        if plugin is None:
            self.renderer.show_error(f"[erro] agente desconhecido: {agent}")
            return None
        connection = self._resolve_plugin_connection(plugin)
        if isinstance(connection, OpenAIConnection):
            return self._call_api(
                agent, plugin, prompt,
                silent=silent,
                show_status=show_status,
                quiet=quiet,
                on_text_chunk=on_text_chunk,
                allow_tools=allow_tools,
            )
        cmd, prompt_as_arg, output_format = self._resolve_plugin_cli_attrs(plugin, connection)
        extra_env = connection.env if isinstance(connection, CliConnection) else None
        cwd = connection.cwd if isinstance(connection, CliConnection) else None
        run_kwargs = {"silent": silent, "agent": agent, "show_status": show_status}
        if extra_env is not None:
            run_kwargs["extra_env"] = extra_env
        if cwd is not None:
            run_kwargs["cwd"] = cwd
        if prompt_as_arg:
            raw = self.run([*cmd, prompt], input_text=None, **run_kwargs)
        else:
            raw = self.run(cmd, input_text=prompt, **run_kwargs)
        fmt = output_format
        if fmt == "stream-json" and raw is not None:
            return parse_stream_json(raw, agent, self.tool_event_callback)
        if fmt == "codex-json" and raw is not None:
            return parse_codex_json(raw, agent, self.tool_event_callback)
        if fmt == "opencode-json" and raw is not None:
            return parse_opencode_json(raw, agent, self.tool_event_callback)
        return raw

    # ------------------------------------------------------------------
    # _call_api() — driver de API (OpenAI compat / Ollama)
    # ------------------------------------------------------------------

    def _call_api(
        self,
        agent,
        plugin,
        prompt,
        silent=False,
        show_status=True,
        quiet=False,
        on_text_chunk=None,
        allow_tools=True,
    ):
        """Executa agentes com driver de API (ex: openai_compat para Ollama)."""
        connection = self._resolve_plugin_connection(plugin)
        if not isinstance(connection, OpenAIConnection):
            self.renderer.show_error(f"[erro] conexão inválida para driver de API: {agent}")
            return None
        is_first_call = agent not in self._api_drivers
        if is_first_call:
            api_key_env = connection.api_key_env
            api_key = os.environ.get(api_key_env, "ollama") if api_key_env else "ollama"
            self._api_drivers[agent] = OpenAICompatDriver(
                model=connection.model,
                base_url=connection.base_url,
                api_key=api_key,
                timeout=self.timeout,
                tool_use_reliability=getattr(plugin, "tool_use_reliability", "medium"),
                extra_body=connection.extra_body,
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

        try:
            with status_cm as status:
                if status is not None:
                    status.update(status_label)
                effective_tool_executor = None
                if allow_tools and getattr(plugin, "supports_tools", True):
                    effective_tool_executor = self.tool_executor
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
                    try:
                        result_holder["result"] = driver_instance.run(
                            prompt=prompt,
                            tool_executor=effective_tool_executor,
                            quiet=quiet,
                            cancel_event=self._cancel_event,
                            on_tool_result=(lambda tool_result: self.tool_event_callback(agent, result=tool_result))
                            if self.tool_event_callback else None,
                            on_tool_abort=(
                                lambda reason: self.tool_event_callback(agent, loop_abort=True, reason=reason))
                            if self.tool_event_callback else None,
                            on_text_chunk=on_text_chunk,
                        )
                    except Exception as exc:
                        result_holder["error"] = exc

                t = threading.Thread(target=_run_driver, daemon=True)
                t.start()

                _api_start = time.time()
                while t.is_alive():
                    if self._cancel_event.is_set():
                        self._user_cancelled = True
                        self.renderer.show_error("[cancelado] pelo usuário")
                        return None
                    time.sleep(0.25)
                    if self.timeout is not None and self.timeout > 0:
                        _api_elapsed = time.time() - _api_start
                        wall_limit = self.timeout * 5
                        if _api_elapsed > wall_limit:
                            self._cancel_event.set()
                            self.renderer.show_error(
                                f"[erro] wall-clock timeout after {int(wall_limit)}s em driver API")
                            return None

                if self._cancel_event.is_set() and result_holder["result"] is None:
                    self._user_cancelled = True
                    return None

                if result_holder["error"]:
                    if _is_rate_limit_signal(str(result_holder["error"])):
                        self.rate_limit_detected = True
                        if self.rate_limit_detected_at is None:
                            self.rate_limit_detected_at = time.time()
                    _cmd = getattr(plugin, "cmd", None)
                    _name = (
                        (_cmd[0] if isinstance(_cmd, (list, tuple)) and _cmd else None)
                        or connection.model or "driver"
                    )
                    self.renderer.show_error(f"[erro] falha ao comunicar com {_name}: {result_holder['error']}")
                    return None

                return result_holder["result"]
        finally:
            self._agent_running = False
            self._stop_esc_monitor()

    # ------------------------------------------------------------------
    # Métricas
    # ------------------------------------------------------------------

    def log_prompt_metrics(
            self, agent, metrics, session_id=None,
            round_index=0, session_call_index=0,
            history_window=12, protocol_mode="standard",
    ):
        """Exibe métricas do prompt e persiste em JSONL quando metrics_file estiver configurado."""
        largest_block = max(
            (
                ("rules", metrics.get("rules_chars", 0)),
                ("session_state", metrics.get("session_state_chars", 0)),
                ("persistent", metrics.get("persistent_chars", 0)),
                ("history", metrics.get("history_chars", 0)),
                ("handoff", metrics.get("handoff_chars", 0)),
            ),
            key=lambda item: item[1],
        )
        self.renderer.show_system(
            "[debug] prompt "
            f"{agent}: total={metrics.get('total_chars', 0)} chars | "
            f"round={round_index} call={session_call_index} | "
            f"history_msgs={metrics.get('history_messages', 0)} | "
            f"primary={metrics.get('primary', True)} | "
            f"rules={metrics.get('rules_chars', 0)} | "
            f"session={metrics.get('session_state_chars', 0)} | "
            f"persistent={metrics.get('persistent_chars', 0)} | "
            f"history={metrics.get('history_chars', 0)} | "
            f"handoff={metrics.get('handoff_chars', 0)} | "
            f"largest_block={largest_block[0]}"
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
