"""Componentes de `quimera.app.system_layer`."""
from __future__ import annotations

from ..constants import CMD_AGENTS, CMD_ALIASES, CMD_CONTEXT, CMD_CONTEXT_EDIT, CMD_HELP, CMD_TASK, build_agents_help, build_help
from ..runtime.parser import strip_tool_block


class AppSystemLayer:
    """Encapsula comandos de sistema e mensagens auxiliares da UI."""

    _SUPPRESSED_TASK_STATUS_FRAGMENTS = (
        ": iniciando",
        ": aguardando review de outro agente",
        ": concluída",
        ": revisando task",
        ": revisando execução de ",
        ": review concluído",
        ": review rejeitado, aguardando outro agente",
    )

    def __init__(self, app):
        """Inicializa uma instância de AppSystemLayer."""
        self.app = app

    def _should_suppress_active_prompt_message(self, message: str) -> bool:
        """Suprime status transitório de task para evitar churn no prompt."""
        if getattr(self.app, "_nonblocking_input_status", None) != "reading":
            return False
        if "\n" in message or not message.startswith("[task "):
            return False
        return any(fragment in message for fragment in self._SUPPRESSED_TASK_STATUS_FRAGMENTS)

    def _should_defer_active_prompt_message(self, message: str) -> bool:
        """Adia mensagens de task enquanto o input TTY estiver ativo."""
        return (
            getattr(self.app, "_nonblocking_input_status", None) == "reading"
            and message.startswith("[task ")
            and "\n" in message
        )

    def flush_deferred_messages(self) -> None:
        """Exibe mensagens de sistema adiadas quando o prompt deixa de estar ativo."""
        deferred = getattr(self.app, "_deferred_system_messages", None)
        if not deferred:
            return
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            deferred.clear()
            return
        with self.app._output_lock:
            for message in deferred:
                renderer.show_system(message)
            deferred.clear()

    def show_system_message(self, message: str) -> None:
        """Exibe system message."""
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            return
        if self._should_suppress_active_prompt_message(message):
            return
        if self._should_defer_active_prompt_message(message):
            self.app._deferred_system_messages.append(message)
            return
        with self.app._output_lock:
            self.app._clear_user_prompt_line_if_needed()
            renderer.show_system(message)
            self.app._redisplay_user_prompt_if_needed(clear_first=False)

    def show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Exibe task response."""
        text = strip_tool_block(response).strip()
        if text:
            self.app.show_system_message(f"[task {task_id}] {agent}:\n{text}")

    def handle_command(self, user_input: str) -> bool:
        """Processa command."""
        command = user_input.strip()
        command = CMD_ALIASES.get(command, command)

        if command == CMD_HELP:
            self.app.renderer.show_system(build_help(self.app.active_agents))
            return True

        if command == CMD_AGENTS:
            self.app.renderer.show_system(build_agents_help(self.app.active_agents))
            return True

        if command.startswith(CMD_TASK):
            self.app.handle_task_command(command)
            return True

        if command == CMD_CONTEXT:
            self.app.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            self.app.context_manager.edit()
            return True

        return False
