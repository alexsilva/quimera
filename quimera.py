import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


class ContextManager:
    """Gerencia o contexto persistente carregado no início de cada rodada."""

    SUMMARY_MARKER = "## Resumo da última sessão"

    def __init__(self, context_file):
        self.context_file = context_file

    def load(self):
        if not self.context_file.exists():
            return ""
        return self.context_file.read_text(encoding="utf-8").strip()

    def show(self):
        context = self.load()
        if not context:
            print("\n[contexto vazio]\n")
            return
        print(f"\n{context}\n")

    def edit(self):
        editor = os.environ.get("EDITOR")
        if not editor:
            print("\nDefina a variável EDITOR para usar /context edit.\n")
            return

        try:
            subprocess.run([editor, str(self.context_file)], check=True)
        except FileNotFoundError:
            print(f"\nEditor não encontrado: {editor}\n")
        except subprocess.CalledProcessError as exc:
            print(f"\nFalha ao abrir o contexto no editor (código {exc.returncode}).\n")

    def update_with_summary(self, summary):
        """Substitui ou cria a seção de resumo curado da última sessão."""
        context = self.load()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_section = f"{self.SUMMARY_MARKER}\n\n_Gerado em {timestamp}_\n\n{summary}"

        if self.SUMMARY_MARKER in context:
            before = context.split(self.SUMMARY_MARKER)[0].rstrip()
            updated = f"{before}\n\n{new_section}"
        else:
            updated = f"{context}\n\n{new_section}"

        self.context_file.write_text(updated.strip() + "\n", encoding="utf-8")
        print(f"[memória] resumo salvo em {self.context_file.name}\n")


class SessionStorage:
    """Centraliza logs textuais e snapshots JSON de uma sessão."""

    def __init__(self, base_dir):
        self.logs_dir = base_dir / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        now = datetime.now()
        self.log_file = self.logs_dir / f"sessao-{now.strftime('%Y-%m-%d')}.txt"
        self.history_file = self.logs_dir / f"sessao-{now.strftime('%Y-%m-%d-%H%M%S')}.json"

    def get_log_file(self):
        return self.log_file

    def get_history_file(self):
        return self.history_file

    def append_log(self, role, content):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.get_log_file().open("a", encoding="utf-8") as file:
            file.write(f"[{timestamp}] [{role.upper()}] {content}\n")

    def save_history(self, history):
        payload = {
            "session_id": self.history_file.stem,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "messages": history,
        }
        with self.history_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def load_last_history(self):
        """Restaura o histórico mais recente salvo em JSON, se existir."""
        json_files = sorted(self.logs_dir.glob("sessao-*.json"), reverse=True)
        if not json_files:
            return []

        latest = json_files[0]
        try:
            with latest.open(encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return []

        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = data.get("messages", [])
        else:
            messages = []

        if messages:
            print(f"[memória] histórico restaurado de {latest.name} ({len(messages)} mensagens)\n")
        return messages


class AgentClient:
    """Executa os agentes externos e encapsula chamadas de resumo."""

    AGENT_CMDS = {
        "claude": lambda prompt: ["claude", "-p", prompt],
        "codex": lambda prompt: ["codex", "exec", "--skip-git-repo-check", prompt],
    }

    def run(self, cmd):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as exc:
            print(f"[erro] comando não encontrado: {cmd[0]} ({exc})")
            return None

        output = result.stdout.strip()
        error = result.stderr.strip()

        if result.returncode != 0:
            print(f"[erro] {' '.join(cmd)} retornou código {result.returncode}")
            if error:
                print(error)
            return None

        if not output:
            if error:
                print(f"[erro] {' '.join(cmd)} não retornou saída válida")
                print(error)
            return None

        return output

    def call(self, agent, prompt):
        """Resolve o comando do agente e delega a execução."""
        build_cmd = self.AGENT_CMDS.get(agent)
        if build_cmd is None:
            print(f"[erro] agente desconhecido: {agent}")
            return None
        return self.run(build_cmd(prompt))

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
        return self.run(["claude", "-p", prompt])


class PromptBuilder:
    """Monta o prompt com contexto persistente e janela recente da conversa."""

    def __init__(self, context_manager, history_window=20):
        self.context_manager = context_manager
        self.history_window = history_window

    def build(self, agent, history):
        """Gera o prompt final enviado ao agente da vez."""
        context = self.context_manager.load()
        base = f"""Você é {agent.upper()} em uma conversa com:
- HUMANO
- CLAUDE
- CODEX

REGRAS:
- Responda como em um chat
- Pode discordar
- Pode comentar respostas anteriores
- Seja direto
"""

        if context:
            base += f"\n\nCONTEXTO PERSISTENTE:\n{context}"

        base += "\n\nCONVERSA:"
        for message in history[-self.history_window:]:
            base += f"\n[{message['role'].upper()}]: {message['content']}"

        base += f"\n[{agent.upper()}]:"
        return base


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.context_manager = ContextManager(base_dir / "quimera_context.md")
        self.storage = SessionStorage(base_dir)
        self.agent_client = AgentClient()
        self.prompt_builder = PromptBuilder(self.context_manager)
        self.history = self.storage.load_last_history()

    def handle_command(self, user_input):
        command = user_input.strip()

        if command == "/context":
            self.context_manager.show()
            return True

        if command == "/context edit":
            self.context_manager.edit()
            return True

        return False

    def parse_routing(self, user_input):
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada."""
        stripped = user_input.lstrip()
        lowered = stripped.lower()

        prefixes = ("/codex", "/claude")
        matched = [p for p in prefixes if lowered == p or lowered.startswith(f"{p} ")]
        if len(matched) > 1:
            print(f"\nUse apenas um prefixo por vez: /claude ou /codex\n")
            return None, None

        for prefix, agent in [("/codex", "codex"), ("/claude", "claude")]:
            if lowered == prefix:
                return agent, ""
            if lowered.startswith(f"{prefix} "):
                return agent, stripped[len(prefix):].lstrip()

        return "claude", user_input

    def call_agent(self, agent):
        prompt = self.prompt_builder.build(agent, self.history)
        return self.agent_client.call(agent, prompt)

    def print_response(self, agent, response):
        label = agent.capitalize()
        if response is not None:
            print(f"\n{label}: {response}\n")
        else:
            print(f"\n{label}: [sem resposta válida]\n")

    def persist_message(self, role, content):
        """Persiste uma mensagem no histórico em memória, log e snapshot JSON."""
        self.history.append({"role": role, "content": content})
        self.storage.append_log(role, content)
        self.storage.save_history(self.history)

    def shutdown(self):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        if not self.history:
            return

        print("\n[memória] histórico salvo. Gerando resumo da sessão...\n")

        summary = self.agent_client.summarize_session(self.history)
        if summary:
            self.context_manager.update_with_summary(summary)
        else:
            print("[memória] não foi possível gerar o resumo.\n")

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        print("Chat multi-agente iniciado (/exit para sair)\n")
        print(f"Log da sessão: {self.storage.get_log_file()}\n")

        try:
            while True:
                user = input("Você: ")

                if user == "/exit":
                    break

                if self.handle_command(user):
                    continue

                first_agent, message = self.parse_routing(user)
                if first_agent is None:
                    continue
                if not message.strip():
                    print(f"\nUse /{first_agent} <mensagem>\n")
                    continue

                second_agent = "codex" if first_agent == "claude" else "claude"

                self.persist_message("human", message)

                for agent in (first_agent, second_agent):
                    response = self.call_agent(agent)
                    self.print_response(agent, response)
                    if response is not None:
                        self.persist_message(agent, response)
        except KeyboardInterrupt:
            print("\nEncerrando chat.")
        finally:
            self.shutdown()


def main():
    """Inicializa e executa a aplicação a partir do diretório do script."""
    app = QuimeraApp(Path(__file__).parent)
    app.run()


if __name__ == "__main__":
    main()
