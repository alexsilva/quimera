"""Componentes de `quimera.app.system_layer`."""
from __future__ import annotations

from .. import plugins
from ..constants import (
    CMD_AGENTS,
    CMD_ALIASES,
    CMD_CLEAR,
    CMD_CONTEXT,
    CMD_CONTEXT_EDIT,
    CMD_HELP,
    CMD_PROMPT,
    CMD_TASK,
    DEFAULT_FIRST_AGENT,
    build_agents_help,
    build_help,
)
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

    def _resolve_prompt_target(self, command: str) -> str | None:
        """Resolve o agente alvo para preview de prompt."""
        raw_target = command[len(CMD_PROMPT):].strip().lower()
        active_agents = list(getattr(self.app, "active_agents", []) or [])

        if not raw_target:
            if DEFAULT_FIRST_AGENT in active_agents:
                return DEFAULT_FIRST_AGENT
            return active_agents[0] if active_agents else None

        normalized = raw_target[1:] if raw_target.startswith("/") else raw_target
        for agent_name in active_agents:
            if normalized == agent_name.lower():
                return agent_name
            plugin = plugins.get(agent_name)
            if plugin is None:
                continue
            candidates = {plugin.prefix.lower().lstrip("/")}
            candidates.update(alias.lower().lstrip("/") for alias in (getattr(plugin, "aliases", None) or []))
            if normalized in candidates:
                return agent_name
        return None

    def _build_prompt_preview_message(self, agent: str) -> str:
        """Monta a saída textual do comando /prompt."""
        history = list(getattr(self.app, "history", []) or [])
        shared_state = getattr(self.app, "shared_state", None)
        prompt_builder = getattr(self.app, "prompt_builder", None)
        if prompt_builder is None:
            raise RuntimeError("prompt_builder indisponível")

        plugin = plugins.get(agent)
        driver = getattr(plugin, "driver", "cli") if plugin else "cli"
        skip_tool_prompt = isinstance(driver, str) and driver != "cli"
        prompt, metrics = prompt_builder.build(
            agent,
            history,
            is_first_speaker=True,
            debug=True,
            primary=True,
            shared_state=shared_state,
            skip_tool_prompt=skip_tool_prompt,
        )
        analysis_lines = [
            f"PROMPT PREVIEW: {agent}",
            f"DRIVER: {driver}",
            f"TOOLS NO TEXTO: {'não' if skip_tool_prompt else 'sim'}",
            "ANÁLISE DOS BLOCOS:",
            f"- regras_chars: {metrics['rules_chars']}",
            f"- session_state_chars: {metrics['session_state_chars']}",
            f"- persistent_chars: {metrics['persistent_chars']}",
            f"- request_chars: {metrics['request_chars']}",
            f"- facts_chars: {metrics['facts_chars']}",
            f"- shared_state_chars: {metrics['shared_state_chars']}",
            f"- history_chars: {metrics['history_chars']}",
            f"- handoff_chars: {metrics['handoff_chars']}",
            f"- history_messages: {metrics['history_messages']}",
            f"- total_chars: {metrics['total_chars']}",
            "",
            "PROMPT FINAL:",
            prompt,
        ]
        return "\n".join(analysis_lines)

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

        if command == CMD_CLEAR:
            self.app.clear_terminal_screen()
            return True

        if command == CMD_PROMPT or command.startswith(f"{CMD_PROMPT} "):
            target = self._resolve_prompt_target(command)
            if target is None:
                self.app.renderer.show_warning("Uso: /prompt [agente]")
                return True
            self.app.renderer.show_system(self._build_prompt_preview_message(target))
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
