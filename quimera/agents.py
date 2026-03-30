import json
import subprocess
import threading
import time
from datetime import datetime, timezone

import quimera.plugins as plugins


class AgentClient:
    """Executa os agentes externos."""

    def __init__(self, renderer, metrics_file=None):
        self.renderer = renderer
        self.metrics_file = metrics_file

    def run(self, cmd, input_text=None):
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            self.renderer.show_error(f"[erro] não foi possível iniciar {cmd[0]}: {exc}")
            return None

        result_holder = {}

        def _communicate():
            try:
                result_holder["stdout"], result_holder["stderr"] = proc.communicate(input=input_text)
            except Exception as exc:
                result_holder["error"] = exc

        thread = threading.Thread(target=_communicate, daemon=True)
        thread.start()

        elapsed = 0
        with self.renderer.running_status("") as status:
            while thread.is_alive():
                if status is not None:
                    status.update(f"[dim]{cmd[0]}... {elapsed}s[/dim]")
                time.sleep(1)
                elapsed += 1

        thread.join()
        proc.wait()

        if "error" in result_holder:
            self.renderer.show_error(f"[erro] falha ao comunicar com {cmd[0]}: {result_holder['error']}")
            return None

        output = result_holder.get("stdout", "").strip()
        error = result_holder.get("stderr", "").strip()

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

    def call(self, agent, prompt):
        """Resolve o comando do agente e delega a execução."""
        plugin = plugins.get(agent)
        if plugin is None:
            self.renderer.show_error(f"[erro] agente desconhecido: {agent}")
            return None
        return self.run(plugin.cmd, input_text=prompt)

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
            with open(self.metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
