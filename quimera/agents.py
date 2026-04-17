"""Componentes de `quimera.agents`."""
from contextlib import nullcontext
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import quimera.plugins as plugins
from quimera.constants import MAX_STDERR_LINES
from quimera.sandbox.bwrap import build_bwrap_cmd
from .runtime.drivers.openai_compat import OpenAICompatDriver

_logger = logging.getLogger(__name__)

_BRALLE_RANGE = re.compile(r'[\u2800-\u28FF]')
_ANSI_ESCAPE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')


class _SyntheticToolResult:
    """Representa uma tool call executada internamente pelo agente CLI."""
    def __init__(self, ok: bool = True, error: str | None = None):
        self.ok = ok
        self.error = error


def _strip_spinner(text: str) -> str:
    """Remove caracteres Braille de spinner do texto."""
    return _BRALLE_RANGE.sub('', text)


def _should_ignore_stderr_line(agent: str | None, line: str) -> bool:
    """Filtra ruído conhecido de stderr que não representa erro real."""
    cleaned = _ANSI_ESCAPE.sub("", _strip_spinner(line)).replace("\r", "").strip()
    return agent == "codex" and cleaned == "Reading additional input from stdin..."


class AgentClient:
    """Executa os agentes externos no diretório de trabalho do projeto."""

    def __init__(self, renderer, metrics_file=None, timeout=None, spy=False, working_dir=None, workspace_root=None, tool_executor=None):
        """Inicializa uma instância de AgentClient."""
        self.renderer = renderer
        self.metrics_file = metrics_file
        self._metrics_lock = threading.Lock()
        self.timeout = timeout
        self.spy = spy
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
        self._user_cancelled = False
        self._agent_running = False
        self._current_proc = None

    def _format_spy_stdout(self, agent: str | None, line: str) -> list[str]:
        """Delega resumo de stdout ao plugin quando ele expõe esse hook."""
        if not agent:
            return []
        plugin = plugins.get(agent)
        formatter = getattr(plugin, "spy_stdout_formatter", None) if plugin else None
        if not callable(formatter):
            return []
        return formatter(line)

    def run(self, cmd, input_text=None, silent=False, agent=None, show_status=True):
        """Executa run."""
        self._cancel_event.clear()
        self._agent_running = True
        self._start_esc_monitor()
        try:
            env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLORTERM": ""}
            effective_cmd = cmd
            if self.execution_mode is not None and self.working_dir:
                effective_cmd = build_bwrap_cmd(
                    self.execution_mode,
                    self.working_dir,
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
                cwd=self.working_dir,
                start_new_session=True,
            )
            self._current_proc = proc
        except OSError as exc:
            self._agent_running = False
            self._stop_esc_monitor()
            self.renderer.show_error(f"[erro] não foi possível iniciar {cmd[0]}: {exc}")
            return None

        result_holder = {"stdout": [], "stderr": [], "error": None}
        last_activity_time = time.time()
        log_queue = queue.Queue() if not silent else None
        stderr_lines_shown = 0  # Contador de linhas de stderr exibidas

        def _read_stdout():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        result_holder["stdout"].append(line)
                        if log_queue is not None and self.spy and agent == "codex":
                            log_queue.put(("stdout", line))
                        nonlocal last_activity_time
                        last_activity_time = time.time()
            except Exception as exc:
                result_holder["error"] = exc

        def _read_stderr():
            try:
                if proc.stderr:
                    for line in proc.stderr:
                        result_holder["stderr"].append(line)
                        if log_queue is not None:
                            log_queue.put(("stderr", line))
                        nonlocal last_activity_time
                        last_activity_time = time.time()
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

        try:
            if silent:
                stdout_thread.join()
                stderr_thread.join()
                if result_holder["stdout"]:
                    _logger.debug("".join(result_holder["stdout"]))
                if result_holder["stderr"]:
                    _logger.warning("".join(result_holder["stderr"]))
            else:
                start_time = time.time()
                elapsed = 0
                assert log_queue is not None

                status_cm = self.renderer.running_status("", agent=agent) if show_status else nullcontext(None)

                with status_cm as status:
                    while stdout_thread.is_alive() or stderr_thread.is_alive() or not log_queue.empty():
                        # Consume log queue in main thread (thread-safe)
                        while not log_queue.empty():
                            try:
                                stream_type, line = log_queue.get_nowait()
                                if status is not None:
                                    status.update(f"[dim]executing {cmd[0]}... {elapsed}s[/dim]")
                                # Limita o número de linhas de stderr exibidas
                                cleaned = _strip_spinner(line.rstrip("\n"))
                                if not cleaned.strip():
                                    continue
                                if stream_type == "stdout":
                                    if self.spy:
                                        for message in self._format_spy_stdout(agent, cleaned):
                                            self.renderer.show_plain(message, agent=agent)
                                    continue
                                if stream_type == "stderr" and _should_ignore_stderr_line(agent, line):
                                    continue
                                if stream_type == "stderr" and not self.spy:
                                    if stderr_lines_shown < MAX_STDERR_LINES:
                                        self.renderer.show_plain(cleaned, agent=agent)
                                        stderr_lines_shown += 1
                                    elif stderr_lines_shown == MAX_STDERR_LINES:
                                        self.renderer.show_plain(f"... (stderr truncado, máximo {MAX_STDERR_LINES} linhas)", agent=agent)
                                        stderr_lines_shown += 1
                                else:
                                    self.renderer.show_plain(cleaned, agent=agent)
                            except queue.Empty:
                                break
                        if status is not None:
                            status.update(f"[dim]executing {cmd[0]}... {elapsed}s[/dim]")
                        if self._cancel_event.is_set():
                            self._terminate_process_group(proc)
                            stdout_thread.join(2)
                            stderr_thread.join(2)
                            self._user_cancelled = True
                            self._agent_running = False
                            self._current_proc = None
                            self._stop_esc_monitor()
                            self.renderer.show_error("[cancelado] pelo usuário")
                            return None
                        time.sleep(0.2)
                        elapsed = int(time.time() - start_time)
                        if self.timeout is not None and self.timeout > 0:
                            if time.time() - last_activity_time > self.timeout:
                                proc.terminate()
                                stdout_thread.join(2)
                                stderr_thread.join(2)
                                self._agent_running = False
                                self._current_proc = None
                                self._stop_esc_monitor()
                                self.renderer.show_error(f"[erro] timeout after {self.timeout}s without output from {cmd[0]}")
                                return None
                stdout_thread.join()
                stderr_thread.join()
                # Drain remaining queue
                while not log_queue.empty():
                    try:
                        stream_type, line = log_queue.get_nowait()
                        cleaned = _strip_spinner(line.rstrip("\n"))
                        if not cleaned.strip():
                            continue
                        if stream_type == "stdout":
                            if self.spy:
                                for message in self._format_spy_stdout(agent, cleaned):
                                    self.renderer.show_plain(message, agent=agent)
                            continue
                        if stream_type == "stderr" and _should_ignore_stderr_line(agent, line):
                            continue
                        # Limita o número de linhas de stderr exibidas
                        if stream_type == "stderr" and not self.spy:
                            if stderr_lines_shown < MAX_STDERR_LINES:
                                self.renderer.show_plain(cleaned, agent=agent)
                                stderr_lines_shown += 1
                            elif stderr_lines_shown == MAX_STDERR_LINES:
                                self.renderer.show_plain(f"... (stderr truncado, máximo {MAX_STDERR_LINES} linhas)")
                                stderr_lines_shown += 1
                        else:
                            self.renderer.show_plain(cleaned, agent=agent)
                    except queue.Empty:
                        break

            proc.wait()
            self._agent_running = False
            self._current_proc = None
            self._stop_esc_monitor()

            if result_holder["error"]:
                self.renderer.show_error(f"[erro] falha ao comunicar com {cmd[0]}: {result_holder['error']}")
                return None

            output = "".join(result_holder["stdout"]).strip()
            error = "".join(result_holder["stderr"]).strip()

        finally:
            self._agent_running = False
            self._stop_esc_monitor()

        if proc.returncode != 0:
            self.renderer.show_error(f"[erro] agente {cmd[0]} retornou código {proc.returncode}")
            # Só mostra o tail se já não excedemos o limite durante o streaming
            if error and stderr_lines_shown <= MAX_STDERR_LINES:
                tail_lines = error.splitlines()[-5:]  # Últimas 5 linhas
                tail = "\n".join(tail_lines)
                self.renderer.show_error(tail)
            return None

        if not output:
            if error:
                self.renderer.show_error(f"[erro] agente {cmd[0]} não retornou saída válida")
                # Só mostra o tail se já não excedemos o limite durante o streaming
                if stderr_lines_shown <= MAX_STDERR_LINES:
                    tail_lines = error.splitlines()[-5:]  # Últimas 5 linhas
                    tail = "\n".join(tail_lines)
                    self.renderer.show_error(tail)
            return None

        return output

    def _terminate_process_group(self, proc):
        """Termina o processo e todo seu grupo (filhos)."""
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass

    def _start_esc_monitor(self):
        """Inicia monitoramento de cancel via signal handler (Ctrl+C)."""
        self._cancel_event.clear()
        if threading.current_thread() is not threading.main_thread():
            self._old_signal_handler = None
            return

        def _signal_handler(signum, frame):
            if signum == signal.SIGINT:
                self._cancel_event.set()

        self._old_signal_handler = signal.signal(signal.SIGINT, _signal_handler)

    def _stop_esc_monitor(self):
        """Para o monitoramento e restaura o signal handler."""
        self._agent_running = False
        if hasattr(self, '_old_signal_handler') and self._old_signal_handler is not None:
            try:
                signal.signal(signal.SIGINT, self._old_signal_handler)
            except Exception:
                pass
            self._old_signal_handler = None

    def _parse_stream_json(self, raw: str, agent: str) -> str | None:
        """Parseia output em stream-json do CLI, extrai texto final e dispara callbacks de tool."""
        result_text = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "result":
                if event.get("is_error"):
                    _logger.warning("[stream-json] agent=%s reported error: %s", agent, event.get("result"))
                    return None
                result_text = event.get("result") or ""
            elif etype == "assistant":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_use" and self.tool_event_callback:
                        tool_name = block.get("name", "unknown")
                        _logger.debug("[stream-json] agent=%s used tool=%s", agent, tool_name)
                        self.tool_event_callback(agent, result=_SyntheticToolResult(ok=True))
        return result_text

    def _parse_codex_json(self, raw: str, agent: str) -> str | None:
        """Parseia output JSONL do `codex exec --json`, extrai último agent_message e registra tool calls."""
        result_text = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "item.completed":
                item = event.get("item", {})
                itype = item.get("type")
                if itype == "agent_message":
                    result_text = item.get("text") or ""
                elif itype == "command_execution" and self.tool_event_callback:
                    cmd = item.get("command", "unknown")
                    ok = item.get("exit_code") == 0
                    _logger.debug("[codex-json] agent=%s ran command=%s ok=%s", agent, cmd, ok)
                    self.tool_event_callback(agent, result=_SyntheticToolResult(ok=ok))
        return result_text

    def call(self, agent, prompt, silent=False, show_status=True, quiet=False):
        """Resolve o comando do agente e delega a execução."""
        self._user_cancelled = False
        if self.execution_mode and self.execution_mode.prompt_addon:
            prompt = f"{self.execution_mode.prompt_addon}\n\n{prompt}"
        plugin = plugins.get(agent)
        if plugin is None:
            self.renderer.show_error(f"[erro] agente desconhecido: {agent}")
            return None
        driver = getattr(plugin, "driver", "cli")
        if isinstance(driver, str) and driver != "cli":
            return self._call_api(agent, plugin, prompt, silent=silent, show_status=show_status, quiet=quiet)
        if plugin.prompt_as_arg:
            raw = self.run([*plugin.cmd, prompt], input_text=None, silent=silent, agent=agent, show_status=show_status)
        else:
            raw = self.run(plugin.cmd, input_text=prompt, silent=silent, agent=agent, show_status=show_status)
        fmt = getattr(plugin, "output_format", None)
        if fmt == "stream-json" and raw is not None:
            return self._parse_stream_json(raw, agent)
        if fmt == "codex-json" and raw is not None:
            return self._parse_codex_json(raw, agent)
        return raw

    def _call_api(self, agent, plugin, prompt, silent=False, show_status=True, quiet=False):
        """Executa agentes com driver de API (ex: openai_compat para Ollama)."""
        is_first_call = agent not in self._api_drivers
        if is_first_call:
            api_key_env = getattr(plugin, "api_key_env", None)
            api_key = os.environ.get(api_key_env, "ollama") if api_key_env else "ollama"
            self._api_drivers[agent] = OpenAICompatDriver(
                model=plugin.model,
                base_url=plugin.base_url,
                api_key=api_key,
                timeout=self.timeout,
                tool_use_reliability=getattr(plugin, "tool_use_reliability", "medium"),
            )

        driver_instance = self._api_drivers[agent]
        self._cancel_event.clear()
        self._agent_running = True
        self._start_esc_monitor()
        status_cm = self.renderer.running_status("", agent=agent) if (show_status and not silent and not quiet) else nullcontext(None)
        status_label = f"[dim]{'conectando' if is_first_call else 'aguardando'} {plugin.model}...[/dim]"

        try:
            with status_cm as status:
                if status is not None:
                    status.update(status_label)
                effective_tool_executor = self.tool_executor if getattr(plugin, "supports_tools", True) else None
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
                            on_tool_abort=(lambda reason: self.tool_event_callback(agent, loop_abort=True, reason=reason))
                            if self.tool_event_callback else None,
                        )
                    except Exception as exc:
                        result_holder["error"] = exc

                t = threading.Thread(target=_run_driver, daemon=True)
                t.start()

                while t.is_alive():
                    if self._cancel_event.is_set():
                        self._user_cancelled = True
                        self.renderer.show_error("[cancelado] pelo usuário")
                        return None
                    time.sleep(0.25)

                if self._cancel_event.is_set() and result_holder["result"] is None:
                    self._user_cancelled = True
                    return None

                if result_holder["error"]:
                    _cmd = getattr(plugin, "cmd", None)
                    _name = (_cmd[0] if isinstance(_cmd, (list, tuple)) and _cmd else None) or getattr(plugin, "model", "driver")
                    self.renderer.show_error(f"[erro] falha ao comunicar com {_name}: {result_holder['error']}")
                    return None

                return result_holder["result"]
        finally:
            self._agent_running = False
            self._stop_esc_monitor()

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
