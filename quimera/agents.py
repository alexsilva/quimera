import subprocess


class AgentClient:
    """Executa os agentes externos e encapsula chamadas de resumo."""

    def __init__(self, renderer):
        self.renderer = renderer

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
