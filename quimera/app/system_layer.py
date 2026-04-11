from __future__ import annotations

from ..constants import CMD_CONTEXT, CMD_CONTEXT_EDIT, CMD_HELP, CMD_TASK, build_help
from ..runtime.parser import strip_tool_block


class AppSystemLayer:
    """Encapsula comandos de sistema e mensagens auxiliares da UI."""

    def __init__(self, app):
        self.app = app

    def show_system_message(self, message: str) -> None:
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            return
        with self.app._output_lock:
            renderer.show_system(message)
            self.app._redisplay_user_prompt_if_needed()

    def show_task_response(self, task_id: int, agent: str, response: str) -> None:
        text = strip_tool_block(response).strip()
        if text:
            self.app.show_system_message(f"[task {task_id}] {agent}:\n{text}")

    def handle_command(self, user_input: str) -> bool:
        command = user_input.strip()

        if command == CMD_HELP:
            self.app.renderer.show_system(build_help(self.app.active_agents))
            return True

        if command.startswith(CMD_TASK):
            self.app._handle_task_command(command)
            return True

        if command == CMD_CONTEXT:
            self.app.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            self.app.context_manager.edit()
            return True

        return False
