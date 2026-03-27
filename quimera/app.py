import re
from pathlib import Path

from .ui import TerminalRenderer
from .context import ContextManager
from .storage import SessionStorage
from .agents import AgentClient
from .prompt import PromptBuilder
from .workspace import Workspace
from .config import ConfigManager
from .constants import (
    EXTEND_MARKER,
    ROUTE_PREFIX,
    CMD_EXIT, CMD_HELP, CMD_CONTEXT, CMD_CONTEXT_EDIT,
    PREFIX_CLAUDE, PREFIX_CODEX,
    AGENT_CLAUDE, AGENT_CODEX, DEFAULT_FIRST_AGENT, AGENT_SEQUENCE,
    USER_ROLE, INPUT_PROMPT,
    MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_HELP, MSG_MIGRATION,
    MSG_MEMORY_SAVING, MSG_MEMORY_FAILED, MSG_SHUTDOWN,
    MSG_DOUBLE_PREFIX, MSG_EMPTY_INPUT,
)


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    ROUTE_PATTERN = re.compile(r"(?m)^\[ROUTE:(claude|codex)\]\s*(.+?)\s*$")

    @staticmethod
    def _format_yes_no(value):
        return "sim" if value else "não"

    def __init__(self, cwd: Path, debug: bool = False, history_window: int | None = None):
        self.renderer = TerminalRenderer()
        self.config = ConfigManager()
        self.user_name = self.config.user_name
        workspace = Workspace(cwd)

        migrated = workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = ContextManager(
            workspace.context_persistent,
            workspace.context_session,
            self.renderer,
        )
        self.storage = SessionStorage(workspace.logs_dir, self.renderer)
        session_id = self.storage.get_history_file().stem
        metrics_file = workspace.metrics_dir / f"{session_id}.jsonl" if debug else None
        self.agent_client = AgentClient(self.renderer, metrics_file=metrics_file)
        self.history = self.storage.load_last_history()
        session_context = self.context_manager.load_session()
        history_restored = bool(self.history)
        summary_loaded = self.context_manager.SUMMARY_MARKER in session_context
        self.session_state = {
            "session_id": session_id,
            "history_count": len(self.history),
            "history_restored": history_restored,
            "summary_loaded": summary_loaded,
        }
        self.debug_prompt_metrics = debug
        self.round_index = 0
        self.session_call_index = 0
        is_new_session = not history_restored and not summary_loaded
        session_state = {
            "session_id": self.session_state["session_id"],
            "is_new_session": self._format_yes_no(is_new_session),
            "history_restored": self._format_yes_no(history_restored),
            "summary_loaded": self._format_yes_no(summary_loaded),
        }
        self.prompt_builder = PromptBuilder(
            self.context_manager,
            history_window=history_window or self.config.history_window,
            session_state=session_state,
            user_name=self.user_name,
        )

    def handle_command(self, user_input):
        command = user_input.strip()

        if command == CMD_HELP:
            self.renderer.show_system(MSG_HELP)
            return True

        if command == CMD_CONTEXT:
            self.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            self.context_manager.edit()
            return True

        return False

    def parse_routing(self, user_input):
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada."""
        stripped = user_input.lstrip()
        lowered = stripped.lower()

        for prefix, agent in AGENT_SEQUENCE:
            if lowered == prefix:
                return agent, ""
            if lowered.startswith(f"{prefix} "):
                message = stripped[len(prefix):].lstrip()
                other_prefix = PREFIX_CLAUDE if prefix == PREFIX_CODEX else PREFIX_CODEX
                lowered_message = message.lower()
                if lowered_message == other_prefix or lowered_message.startswith(f"{other_prefix} "):
                    self.renderer.show_warning(MSG_DOUBLE_PREFIX)
                    return None, None
                return agent, message

        return DEFAULT_FIRST_AGENT, user_input

    def call_agent(self, agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard"):
        self.session_call_index += 1
        if self.debug_prompt_metrics:
            prompt, metrics = self.prompt_builder.build(
                agent,
                self.history,
                is_first_speaker,
                handoff,
                debug=True,
                primary=primary,
            )
            self.agent_client.log_prompt_metrics(
                agent, metrics,
                session_id=self.session_state["session_id"],
                round_index=self.round_index,
                session_call_index=self.session_call_index,
                history_window=self.prompt_builder.history_window,
                protocol_mode=protocol_mode,
            )
        else:
            prompt = self.prompt_builder.build(agent, self.history, is_first_speaker, handoff, primary=primary)
        return self.agent_client.call(agent, prompt)

    def parse_response(self, response):
        """Extrai marcadores de controle e retorna (clean, route_target, handoff, extend)."""
        route_target, handoff = None, None

        if response and ROUTE_PREFIX in response:
            match = self.ROUTE_PATTERN.search(response)
            if match:
                route_target = match.group(1)
                handoff = match.group(2).strip()
                response = self.ROUTE_PATTERN.sub("", response, count=1).strip()

        extend = False
        if response and response.rstrip().endswith(EXTEND_MARKER):
            response = response.rstrip()[: -len(EXTEND_MARKER)].rstrip()
            extend = True

        return response, route_target, handoff, extend

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

        self.renderer.show_system(MSG_MEMORY_SAVING)

        summary = self.agent_client.summarize_session(self.history)
        if summary:
            self.context_manager.update_with_summary(summary)
        else:
            self.renderer.show_system(MSG_MEMORY_FAILED)

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        self.renderer.show_system(MSG_CHAT_STARTED)
        self.renderer.show_system(
            MSG_SESSION_STATUS.format(
                session_id=self.session_state["session_id"],
                history_count=self.session_state["history_count"],
                summary_loaded=self._format_yes_no(self.session_state["summary_loaded"]),
            )
        )
        self.renderer.show_system(MSG_SESSION_LOG.format(self.storage.get_log_file()))

        try:
            while True:
                user = input(f"{self.user_name}: ")

                if user == CMD_EXIT:
                    break

                if self.handle_command(user):
                    continue

                first_agent, message = self.parse_routing(user)
                if first_agent is None:
                    continue
                if not message.strip():
                    self.renderer.show_warning(MSG_EMPTY_INPUT.format(first_agent))
                    continue

                second_agent = AGENT_CODEX if first_agent == AGENT_CLAUDE else AGENT_CLAUDE

                self.round_index += 1
                self.persist_message(USER_ROLE, message)

                # Primeira fala: detecta se o agente quer debate estendido
                response = self.call_agent(first_agent, is_first_speaker=True, protocol_mode="standard")
                response, route_target, handoff, extend = self.parse_response(response)
                self.print_response(first_agent, response)
                if response is not None:
                    self.persist_message(first_agent, response)

                # Fluxo padrão: 2 falas. Estendido (EXTEND_MARKER): 4 falas alternadas.
                protocol_mode = "extended" if extend else "standard"
                remaining = [second_agent, first_agent, second_agent] if extend else [second_agent]
                if route_target and remaining:
                    remaining[0] = route_target

                next_handoff = handoff
                for index, agent in enumerate(remaining):
                    response = self.call_agent(agent, handoff=next_handoff, primary=False, protocol_mode=protocol_mode)
                    next_handoff = None
                    response, route_target, handoff, _ = self.parse_response(response)
                    self.print_response(agent, response)
                    if response is not None:
                        self.persist_message(agent, response)
                    if route_target and index + 1 < len(remaining):
                        remaining[index + 1] = route_target
                    if route_target:
                        next_handoff = handoff
        except KeyboardInterrupt:
            self.renderer.show_system(MSG_SHUTDOWN)
        finally:
            self.shutdown()
