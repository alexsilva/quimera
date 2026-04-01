import json
import subprocess
import threading
import time
from datetime import datetime, timezone

import quimera.plugins as plugins


class AgentClient:
    """Executa os agentes externos."""

    def __init__(self, renderer, metrics_file=None, timeout=None):
        self.renderer = renderer
        self.metrics_file = metrics_file
        self._metrics_lock = threading.Lock()
        self.timeout = timeout

    def run(self, cmd, input_text=None, silent=False):
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.renderer.show_error(f"[erro] não foi possível iniciar {cmd[0]}: {exc}")
            return None

        result_holder = {"stdout": [], "stderr": [], "error": None}
        last_activity_time = time.time()

        def _read_stdout():
            try:
                if proc.stdout:
                    for line in proc.stdout:
                        result_holder["stdout"].append(line)
                        nonlocal last_activity_time
                        last_activity_time = time.time()
            except Exception as exc:
                result_holder["error"] = exc

        def _read_stderr():
            try:
                if proc.stderr:
                    for line in proc.stderr:
                        result_holder["stderr"].append(line)
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
            if proc.stdin:
                proc.stdin.close()
        except Exception as exc:
            self.renderer.show_error(f"[erro] falha ao enviar input para {cmd[0]}: {exc}")
            proc.kill()
            return None

        if silent:
            stdout_thread.join()
            stderr_thread.join()
        else:
            elapsed = 0
            with self.renderer.running_status("") as status:
                while stdout_thread.is_alive() or stderr_thread.is_alive():
                    if status is not None:
                        status.update(f"[dim]{cmd[0]}... {elapsed}s[/dim]")
                    time.sleep(1)
                    elapsed += 1
                    if self.timeout is not None and self.timeout > 0:
                        if time.time() - last_activity_time > self.timeout:
                            proc.terminate()
                            stdout_thread.join(2)
                            stderr_thread.join(2)
                            self.renderer.show_error(f"[erro] timeout after {self.timeout}s without output from {cmd[0]}")
                            return None
            stdout_thread.join()
            stderr_thread.join()

        proc.wait()

        if result_holder["error"]:
            self.renderer.show_error(f"[erro] falha ao comunicar com {cmd[0]}: {result_holder['error']}")
            return None

        output = "".join(result_holder["stdout"]).strip()
        error = "".join(result_holder["stderr"]).strip()

        if proc.returncode != 0:
            self.renderer.show_error(f"[erro] {' '.join(cmd)} retornou código {proc.returncode}")
            if error:
                tail = "\n".join(error.splitlines()[-5:])
                self.renderer.show_error(tail)
            return None

        if not output:
            if error:
                self.renderer.show_error(f"[erro] {' '.join(cmd)} não retornou saída válida")
                tail = "\n".join(error.splitlines()[-5:])
                self.renderer.show_error(tail)
            return None

        return output

    def call(self, agent, prompt, silent=False):
        """Resolve o comando do agente e delega a execução."""
        plugin = plugins.get(agent)
        if plugin is None:
            self.renderer.show_error(f"[erro] agente desconhecido: {agent}")
            return None
        if plugin.prompt_as_arg:
            return self.run([*plugin.cmd, prompt], input_text=None, silent=silent)
        return self.run(plugin.cmd, input_text=prompt, silent=silent)

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
