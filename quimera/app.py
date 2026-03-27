from pathlib import Path

from .ui import TerminalRenderer
from .context import ContextManager
from .storage import SessionStorage
from .agents import AgentClient
from .prompt import PromptBuilder
from .workspace import Workspace


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""

    def __init__(self, cwd: Path):
        self.renderer = TerminalRenderer()
        workspace = Workspace(cwd)

        migrated = workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(f"[migração] {item}\n")

        self.context_manager = ContextManager(
            workspace.context_persistent,
            workspace.context_session,
            self.renderer,
        )
        self.storage = SessionStorage(workspace.logs_dir, self.renderer)
        self.agent_client = AgentClient(self.renderer)
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
            self.renderer.show_warning("\nUse apenas um prefixo por vez: /claude ou /codex\n")
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
        if response is not None:
            self.renderer.show_message(agent, response)
        else:
            self.renderer.show_no_response(agent)

    def persist_message(self, role, content):
        """Persiste uma mensagem no histórico em memória, log e snapshot JSON."""
        self.history.append({"role": role, "content": content})
        self.storage.append_log(role, content)
        self.storage.save_history(self.history)

    def shutdown(self):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        if not self.history:
            return

        self.renderer.show_system("\n[memória] histórico salvo. Gerando resumo da sessão...\n")

        summary = self.agent_client.summarize_session(self.history)
        if summary:
            self.context_manager.update_with_summary(summary)
        else:
            self.renderer.show_system("[memória] não foi possível gerar o resumo.\n")

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        self.renderer.show_system("Chat multi-agente iniciado (/exit para sair)\n")
        self.renderer.show_system(f"Log da sessão: {self.storage.get_log_file()}\n")

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
                    self.renderer.show_warning(f"\nUse /{first_agent} <mensagem>\n")
                    continue

                second_agent = "codex" if first_agent == "claude" else "claude"

                self.persist_message("human", message)

                for agent in (first_agent, second_agent):
                    response = self.call_agent(agent)
                    self.print_response(agent, response)
                    if response is not None:
                        self.persist_message(agent, response)
        except KeyboardInterrupt:
            self.renderer.show_system("\nEncerrando chat.")
        finally:
            self.shutdown()
