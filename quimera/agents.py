import json
import subprocess
from datetime import datetime, timezone


class AgentClient:
    """Executa os agentes externos e encapsula chamadas de resumo."""

    def __init__(self, renderer, metrics_file=None):
        self.renderer = renderer
        self.metrics_file = metrics_file

    AGENT_CMDS = {
        "claude": ["claude", "-p"],
        "codex": ["codex", "exec", "--skip-git-repo-check"],
    }

    def run(self, cmd, input_text=None):
        try:
            result = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            self.renderer.show_error(f"[erro] comando não encontrado: {cmd[0]} ({exc})")
            return None

        output = result.stdout.strip()
        error = result.stderr.strip()

        if result.returncode != 0:
            self.renderer.show_error(f"[erro] {' '.join(cmd)} retornou código {result.returncode}")
            if error:
                self.renderer.show_error(error)
            return None

        if not output:
            if error:
                self.renderer.show_error(f"[erro] {' '.join(cmd)} não retornou saída válida")
                self.renderer.show_error(error)
            return None

        return output

    def call(self, agent, prompt):
        """Resolve o comando do agente e delega a execução."""
        cmd = self.AGENT_CMDS.get(agent)
        if cmd is None:
            self.renderer.show_error(f"[erro] agente desconhecido: {agent}")
            return None
        return self.run(cmd, input_text=prompt)

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

    def summarize_session(self, history):
        """Pede ao Claude um resumo curto para atualizar o contexto persistente."""
        if not history:
            return None

        conversation = "\n".join(
            f"[{message['role'].upper()}]: {message['content']}" for message in history
        )
        prompt = f"""Você é um assistente de memória. Analise a conversa abaixo e gere um resumo estruturado em markdown.

O resumo deve conter:
- O que foi discutido (tópicos principais)
- Decisões tomadas (se houver)
- Pendências ou próximos passos (se houver)

Seja conciso. Máximo 20 linhas. Não use emojis. Escreva em português.

CONVERSA:
{conversation}

RESUMO:"""
        return self.run(["claude", "-p"], input_text=prompt)
