"""Componentes de `quimera.app.system_layer`."""
from __future__ import annotations
import shlex

from ..constants import (
    CMD_AGENTS,
    CMD_ALIASES,
    CMD_CLEAR,
    CMD_CONNECT,
    CMD_CONTEXT,
    CMD_CONTEXT_EDIT,
    CMD_HELP,
    CMD_PROMPT,
    CMD_RELOAD,
    CMD_TASK,
    DEFAULT_FIRST_AGENT,
    build_agents_help,
    build_help,
)
from ..plugins.base import CliConnection, OpenAIConnection, format_connection_label, reload_plugins, set_connection_override
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
            self.show_system_message(f"[task {task_id}] {agent}:\n{text}")

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
            plugin = self.app.get_agent_plugin(agent_name)
            if plugin is None:
                continue
            candidates = {plugin.prefix.lower().lstrip("/")}
            candidates.update(alias.lower().lstrip("/") for alias in (getattr(plugin, "aliases", None) or []))
            if normalized in candidates:
                return agent_name
        return None

    def _resolve_connect_target(self, command: str) -> str | None:
        """Resolve o agente alvo para configuração de conexão."""
        raw_target = command[len(CMD_CONNECT):].strip().lower()
        if not raw_target:
            return None

        normalized = raw_target[1:] if raw_target.startswith("/") else raw_target
        for plugin in self.app.get_available_plugins():
            if normalized == plugin.name.lower():
                return plugin.name
            candidates = {plugin.prefix.lower().lstrip("/")}
            candidates.update(alias.lower().lstrip("/") for alias in (getattr(plugin, "aliases", None) or []))
            if normalized in candidates:
                return plugin.name
        return None

    def _read_command_input(self, prompt: str) -> str | None:
        """Lê input síncrono para comandos interativos do chat."""
        reader = getattr(self.app, "read_user_input", None)
        if callable(reader):
            return reader(prompt, timeout=-1)
        return input(prompt)

    def _prompt_text(self, label: str, default: str | None = None) -> str:
        """Solicita texto com default opcional."""
        suffix = f" [{default}]" if default not in {None, ""} else ""
        value = self._read_command_input(f"{label}{suffix}: ")
        value = (value or "").strip()
        if value:
            return value
        return default or ""

    def _prompt_bool(self, label: str, default: bool = False) -> bool:
        """Solicita um booleano interativamente."""
        default_label = "s" if default else "n"
        while True:
            raw = self._read_command_input(f"{label} [s/n] [{default_label}]: ")
            raw = (raw or "").strip().lower()
            if not raw:
                return default
            if raw in {"s", "sim", "y", "yes"}:
                return True
            if raw in {"n", "nao", "não", "no"}:
                return False
            self.app.renderer.show_warning("Valor inválido. Use 's' ou 'n'.")

    def _configure_connection_interactively(self, plugin):
        """Coleta configuração de conexão de forma interativa no chat."""
        current = plugin.effective_connection()
        current_driver = "cli" if isinstance(current, CliConnection) else "openai"
        driver = self._prompt_text("Driver", current_driver).strip().lower()
        while driver not in {"cli", "openai"}:
            self.app.renderer.show_warning("Driver inválido. Use 'cli' ou 'openai'.")
            driver = self._prompt_text("Driver", current_driver).strip().lower()

        if driver == "cli":
            cli_defaults = current if isinstance(current, CliConnection) else CliConnection(cmd=list(plugin.cmd))
            cmd_default = " ".join(cli_defaults.cmd) if cli_defaults.cmd else ""
            cmd_text = self._prompt_text("Comando", cmd_default)
            if not cmd_text:
                raise ValueError("Configuração cancelada: comando CLI vazio.")
            return CliConnection(
                cmd=shlex.split(cmd_text),
                prompt_as_arg=self._prompt_bool("Enviar prompt como argumento", cli_defaults.prompt_as_arg),
                output_format=cli_defaults.output_format,
            )

        api_defaults = current if isinstance(current, OpenAIConnection) else OpenAIConnection(
            model=plugin.model or "gpt-4o",
            base_url=plugin.base_url or "https://api.openai.com/v1",
            api_key_env=plugin.api_key_env or "OPENAI_API_KEY",
            provider=plugin.driver if plugin.driver != "cli" else "openai_compat",
            supports_native_tools=plugin.supports_tools,
        )
        provider_default = api_defaults.provider if api_defaults.provider != "openai" else "openai_compat"
        return OpenAIConnection(
            model=self._prompt_text("Modelo", api_defaults.model) or api_defaults.model,
            base_url=self._prompt_text("Base URL", api_defaults.base_url) or api_defaults.base_url,
            api_key_env=self._prompt_text("Variável da API key", api_defaults.api_key_env) or api_defaults.api_key_env,
            provider=provider_default,
            supports_native_tools=api_defaults.supports_native_tools,
        )

    def _build_prompt_preview_message(self, agent: str) -> str:
        """Monta a saída textual do comando /prompt."""
        history = list(getattr(self.app, "history", []) or [])
        shared_state = getattr(self.app, "shared_state", None)
        prompt_builder = getattr(self.app, "prompt_builder", None)
        if prompt_builder is None:
            raise RuntimeError("prompt_builder indisponível")

        plugin = self.app.get_agent_plugin(agent)
        driver = plugin.effective_driver() if plugin else "cli"
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

        if command == CMD_CONNECT or command.startswith(f"{CMD_CONNECT} "):
            target = self._resolve_connect_target(command)
            if target is None:
                self.app.renderer.show_warning("Uso: /connect <agente>")
                return True
            plugin = self.app.get_agent_plugin(target)
            if plugin is None:
                self.app.renderer.show_warning(f"Agente desconhecido: {target}")
                return True
            self.show_system_message(f"Configurando conexão para {target}")
            self.show_system_message(f"Atual: {format_connection_label(plugin.effective_connection())}")
            try:
                connection = self._configure_connection_interactively(plugin)
            except ValueError as exc:
                self.app.renderer.show_warning(str(exc))
                return True
            set_connection_override(target, connection, persist=True)
            self.show_system_message(f"Conexão ativa para {target}: {format_connection_label(connection)}")
            return True

        if command == CMD_CLEAR:
            self.app.clear_terminal_screen()
            return True

        if command == CMD_RELOAD:
            names = reload_plugins()
            self.app.active_agents = names
            self.app.selected_agents = names
            self.app.renderer.show_system(f"Plugins recarregados: {len(names)} agentes disponíveis")
            return True

        if command == CMD_PROMPT or command.startswith(f"{CMD_PROMPT} "):
            target = self._resolve_prompt_target(command)
            if target is None:
                self.app.renderer.show_warning("Uso: /prompt [agente]")
                return True
            self.app.renderer.show_system(self._build_prompt_preview_message(target))
            return True

        if command.startswith(CMD_TASK):
            self.app.task_services.handle_task_command(command)
            return True

        if command == CMD_CONTEXT:
            self.app.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            self.app.context_manager.edit()
            return True

        return False
