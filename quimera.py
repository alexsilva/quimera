import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


class ContextManager:
    SUMMARY_MARKER = "## Resumo da ultima sessao"

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
            print("\nDefina a variavel EDITOR para usar /context edit.\n")
            return

        try:
            subprocess.run([editor, str(self.context_file)], check=True)
        except FileNotFoundError:
            print(f"\nEditor nao encontrado: {editor}\n")
        except subprocess.CalledProcessError as exc:
            print(f"\nFalha ao abrir o contexto no editor (codigo {exc.returncode}).\n")

    def update_with_summary(self, summary):
        context = self.load()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_section = f"{self.SUMMARY_MARKER}\n\n_Gerado em {timestamp}_\n\n{summary}"

        if self.SUMMARY_MARKER in context:
            before = context.split(self.SUMMARY_MARKER)[0].rstrip()
            updated = f"{before}\n\n{new_section}"
        else:
            updated = f"{context}\n\n{new_section}"

        self.context_file.write_text(updated.strip() + "\n", encoding="utf-8")
        print(f"[memoria] resumo salvo em {self.context_file.name}\n")


class SessionStorage:
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
            print(f"[memoria] historico restaurado de {latest.name} ({len(messages)} mensagens)\n")
        return messages


class AgentClient:
    AGENT_CMDS = {
        "claude": lambda prompt: ["claude", "-p", prompt],
        "codex": lambda prompt: ["codex", "exec", "--skip-git-repo-check", prompt],
    }

    def run(self, cmd):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as exc:
            print(f"[erro] comando nao encontrado: {cmd[0]} ({exc})")
            return None

        output = result.stdout.strip()
        error = result.stderr.strip()

        if result.returncode != 0:
            print(f"[erro] {' '.join(cmd)} retornou codigo {result.returncode}")
            if error:
                print(error)
            return None

        if not output:
            if error:
                print(f"[erro] {' '.join(cmd)} nao retornou saida valida")
                print(error)
            return None

        return output

    def call(self, agent, prompt):
        build_cmd = self.AGENT_CMDS.get(agent)
        if build_cmd is None:
            print(f"[erro] agente desconhecido: {agent}")
            return None
        return self.run(build_cmd(prompt))

    def summarize_session(self, history):
        if not history:
            return None

        conversation = "\n".join(
            f"[{message['role'].upper()}]: {message['content']}" for message in history
        )
        prompt = f"""Voce e um assistente de memoria. Analise a conversa abaixo e gere um resumo estruturado em markdown.

O resumo deve conter:
- O que foi discutido (topicos principais)
- Decisoes tomadas (se houver)
- Pendencias ou proximos passos (se houver)

Seja conciso. Maximo 20 linhas. Nao use emojis. Escreva em portugues.

CONVERSA:
{conversation}

RESUMO:"""
        return self.run(["claude", "-p", prompt])


class PromptBuilder:
    def __init__(self, context_manager, history_window=20):
        self.context_manager = context_manager
        self.history_window = history_window

    def build(self, agent, history):
        context = self.context_manager.load()
        base = f"""Voce e {agent.upper()} em uma conversa com:
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
            print(f"\n{label}: [sem resposta valida]\n")

    def persist_message(self, role, content):
        self.history.append({"role": role, "content": content})
        self.storage.append_log(role, content)
        self.storage.save_history(self.history)

    def shutdown(self):
        if not self.history:
            return

        print("\n[memoria] historico salvo. Gerando resumo da sessao...\n")

        summary = self.agent_client.summarize_session(self.history)
        if summary:
            self.context_manager.update_with_summary(summary)
        else:
            print("[memoria] nao foi possivel gerar o resumo.\n")

    def run(self):
        print("Chat multi-agente iniciado (/exit para sair)\n")
        print(f"Log da sessao: {self.storage.get_log_file()}\n")

        try:
            while True:
                user = input("Voce: ")

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
    app = QuimeraApp(Path(__file__).parent)
    app.run()


if __name__ == "__main__":
    main()
