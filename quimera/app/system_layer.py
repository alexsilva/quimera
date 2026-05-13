"""Componentes de `quimera.app.system_layer`."""
from __future__ import annotations
import json
import shlex
import threading
from contextlib import nullcontext

from ..constants import (
    CMD_AGENTS,
    CMD_ALIASES,
    CMD_APPROVE,
    CMD_APPROVE_ALL,
    CMD_CLEAR,
    CMD_CONNECT,
    CMD_CONTEXT,
    CMD_DISCONNECT,
    CMD_CONTEXT_BRANCH,
    CMD_CONTEXT_EDIT,
    CMD_HELP,
    CMD_PROMPT,
    CMD_RELOAD,
    CMD_RESET_STATE,
    CMD_TASK,
    DEFAULT_FIRST_AGENT,
    build_agents_help,
    build_help,
)
from ..plugins import remove_connection
from .. import plugins as _plugins
from ..plugins.base import (
    CliConnection,
    OpenAIConnection,
    format_connection_label,
    get_connection_overrides,
    is_valid_agent_name,
    register_dynamic_plugin,
    reload_plugins,
    set_connection_override,
)
from ..runtime.parser import strip_tool_block


class AppSystemLayer:
    """Encapsula comandos de sistema e mensagens auxiliares da UI."""

    _SUPPRESSED_TASK_STATUS_FRAGMENTS = (
        ": iniciando",
        ": aguardando review de outro agente",
        ": revisando task",
        ": revisando execução de ",
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
        """Adia mensagens de task enquanto o input TTY estiver ativo.

        Mensagens com \n contêm o resultado da task (resposta do agente
        ou review) e devem ser exibidas imediatamente, não adiadas.
        Mensagens de conclusão de task também são exibidas imediatamente.
        """
        return (
                getattr(self.app, "_nonblocking_input_status", None) == "reading"
                and message.startswith("[task ")
                and "\n" not in message
                and ": concluída" not in message
                and ": review concluído" not in message
        )

    def _is_prompt_active(self) -> bool:
        """Retorna se há um prompt interativo ativo no momento."""
        return getattr(self.app, "_nonblocking_input_status", None) == "reading"

    def _is_prompt_owner_thread(self) -> bool:
        """Retorna se a thread atual é a dona do prompt interativo."""
        current_thread_id = getattr(self.app, "_prompt_owning_thread_id", None)
        return current_thread_id is not None and current_thread_id == threading.get_ident()

    def _is_foreign_prompt_thread(self) -> bool:
        """Retorna se outra thread é a dona do prompt interativo."""
        current_thread_id = getattr(self.app, "_prompt_owning_thread_id", None)
        return current_thread_id is not None and current_thread_id != threading.get_ident()

    def _enqueue_deferred_message(self, message: str, level: str = "system") -> bool:
        """Enfileira mensagem diferida preservando o tipo visual."""
        deferred_list = getattr(self.app, "_deferred_system_messages", None)
        if deferred_list is None:
            return False
        max_deferred = getattr(self.app, "_MAX_DEFERRED_SYSTEM_MESSAGES", 100)
        if len(deferred_list) >= max_deferred:
            overflow = len(deferred_list) - max_deferred + 1
            del deferred_list[:overflow]
        deferred_list.append((level, message))
        return True

    def flush_deferred_messages(self) -> None:
        """Exibe mensagens de sistema adiadas quando o prompt deixa de estar ativo."""
        deferred = getattr(self.app, "_deferred_system_messages", None)
        if not deferred:
            return
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            deferred.clear()
            return
        output_lock = getattr(self.app, "_output_lock", nullcontext())
        with output_lock:
            for item in deferred:
                if isinstance(item, tuple) and len(item) == 2:
                    level, message = item
                else:
                    level, message = "system", item
                if level == "neutral" and hasattr(renderer, "show_system_neutral"):
                    renderer.show_system_neutral(message)
                elif level == "warning" and hasattr(renderer, "show_warning"):
                    renderer.show_warning(message)
                elif level == "error" and hasattr(renderer, "show_error"):
                    renderer.show_error(message)
                else:
                    renderer.show_system(message)
            flush = getattr(renderer, "flush", None)
            if callable(flush):
                flush()
            deferred.clear()

    def show_system_message(self, message: str) -> None:
        """Exibe system message."""
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            return
        if self._should_suppress_active_prompt_message(message):
            return
        if self._should_defer_active_prompt_message(message):
            if self._enqueue_deferred_message(message, level="system"):
                return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._enqueue_deferred_message(message, level="system"):
                return
        output_lock = getattr(self.app, "_output_lock", nullcontext())
        with output_lock:
            # Clear prompt line only if we're the thread that owns the prompt
            current_thread_id = getattr(self.app, '_prompt_owning_thread_id', None)
            if current_thread_id is None or current_thread_id == threading.get_ident():
                self.app._clear_user_prompt_line_if_needed()
            renderer.show_system(message)
            flush = getattr(renderer, "flush", None)
            if callable(flush):
                flush()
            # Redisplay prompt only if we're the thread that owns the prompt
            if current_thread_id is None or current_thread_id == threading.get_ident():
                self.app._redisplay_user_prompt_if_needed(clear_first=False)

    def show_muted_message(self, message: str) -> None:
        """Exibe mensagem em estilo neutro (dim) via writer thread do renderer.

        Toda escrita de terminal passa pela fila do writer thread para evitar
        conflito com o estado interno do prompt_toolkit. Background threads
        nunca escrevem diretamente no stdout.
        """
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._enqueue_deferred_message(message, level="neutral"):
                return
        output_lock = getattr(self.app, "_output_lock", nullcontext())
        with output_lock:
            current_thread_id = getattr(self.app, '_prompt_owning_thread_id', None)
            is_owning = current_thread_id is not None and current_thread_id == threading.get_ident()
            if is_owning:
                self.app._clear_user_prompt_line_if_needed()
            elif not is_owning:
                show_newline = getattr(renderer, "show_newline", None)
                if callable(show_newline):
                    show_newline()
            show_system_neutral = getattr(renderer, "show_system_neutral", None)
            if callable(show_system_neutral):
                show_system_neutral(message)
            else:
                renderer.show_system(message)
            flush = getattr(renderer, "flush", None)
            if callable(flush):
                flush()
            if is_owning:
                self.app._redisplay_user_prompt_if_needed(clear_first=False)

    def show_warning_message(self, message: str) -> None:
        """Exibe warning de forma compatível com prompt ativo e background threads."""
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._enqueue_deferred_message(message, level="warning"):
                return
        output_lock = getattr(self.app, "_output_lock", nullcontext())
        with output_lock:
            current_thread_id = getattr(self.app, '_prompt_owning_thread_id', None)
            is_owning = current_thread_id is not None and current_thread_id == threading.get_ident()
            if is_owning:
                self.app._clear_user_prompt_line_if_needed()
            show_warning = getattr(renderer, "show_warning", None)
            if callable(show_warning):
                show_warning(message)
            else:
                renderer.show_system(message)
            flush = getattr(renderer, "flush", None)
            if callable(flush):
                flush()
            if is_owning:
                self.app._redisplay_user_prompt_if_needed(clear_first=False)

    def show_error_message(self, message: str) -> None:
        """Exibe error de forma compatível com prompt ativo e background threads."""
        renderer = getattr(self.app, "renderer", None)
        if renderer is None:
            return
        if self._is_prompt_active() and self._is_foreign_prompt_thread():
            if self._enqueue_deferred_message(message, level="error"):
                return
        output_lock = getattr(self.app, "_output_lock", nullcontext())
        with output_lock:
            current_thread_id = getattr(self.app, '_prompt_owning_thread_id', None)
            is_owning = current_thread_id is not None and current_thread_id == threading.get_ident()
            if is_owning:
                self.app._clear_user_prompt_line_if_needed()
            show_error = getattr(renderer, "show_error", None)
            if callable(show_error):
                show_error(message)
            else:
                renderer.show_system(message)
            flush = getattr(renderer, "flush", None)
            if callable(flush):
                flush()
            if is_owning:
                self.app._redisplay_user_prompt_if_needed(clear_first=False)

    def list_connected_agents(self) -> list[str]:
        """Retorna nomes dos agentes com conexão persistida."""
        return sorted(get_connection_overrides().keys())

    def show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Exibe task response."""
        text = strip_tool_block(response).strip()
        if text:
            self.show_muted_message(f"[task {task_id}] {agent}:\n{text}")

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
        return normalized if is_valid_agent_name(normalized) else None

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
        """Coleta configuração de conexão de forma interativa no chat.

        Retorna (connection, base_plugin_name | None).
        """
        base_name = self._prompt_text("Plugin base (enter para ignorar)", "").strip().lower()
        if base_name:
            base_plugin = _plugins.get(base_name)
            if base_plugin is None:
                raise ValueError(f"Plugin base '{base_name}' não encontrado.")
            model_id = self._prompt_text("Modelo", "").strip()
            if not model_id:
                raise ValueError("Configuração cancelada: modelo vazio.")
            return base_plugin.configure_with_model(model_id), base_plugin.name

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
            ), None

        api_defaults = current if isinstance(current, OpenAIConnection) else OpenAIConnection(
            model=plugin.model or "gpt-4o",
            base_url=plugin.base_url or "https://api.openai.com/v1",
            api_key_env=plugin.api_key_env or "OPENAI_API_KEY",
            provider=plugin.driver if plugin.driver != "cli" else "openai_compat",
            supports_native_tools=plugin.supports_tools,
            extra_body=getattr(current, "extra_body", None),
        )
        provider_default = api_defaults.provider if api_defaults.provider != "openai" else "openai_compat"
        extra_body_raw = self._prompt_text("extra_body (JSON, enter para ignorar)", "").strip()
        extra_body = None
        if extra_body_raw:
            try:
                extra_body = json.loads(extra_body_raw)
                # Se o JSON for vazio ({}), trata como "limpar extra_body"
                if extra_body == {}:
                    extra_body = None
            except json.JSONDecodeError as exc:
                self.app.renderer.show_warning(f"JSON inválido: {exc}. extra_body será ignorado.")
                extra_body = api_defaults.extra_body
        else:
            # Enter vazio = preserva o valor anterior
            extra_body = api_defaults.extra_body
        conn = OpenAIConnection(
            model=self._prompt_text("Modelo", api_defaults.model) or api_defaults.model,
            base_url=self._prompt_text("Base URL", api_defaults.base_url) or api_defaults.base_url,
            api_key_env=self._prompt_text("Variável da API key", api_defaults.api_key_env) or api_defaults.api_key_env,
            provider=provider_default,
            supports_native_tools=api_defaults.supports_native_tools,
            extra_body=extra_body,
        )
        return conn, None

    def _build_prompt_preview_message(self, agent: str) -> str:
        """Monta a saída textual do comando /prompt."""
        history = list(getattr(self.app, "history", []) or [])
        shared_state = getattr(self.app, "shared_state", None)
        prompt_builder = getattr(self.app, "prompt_builder", None)
        if prompt_builder is None:
            raise RuntimeError("prompt_builder indisponível")

        plugin = self.app.get_agent_plugin(agent)
        driver = plugin.effective_driver() if plugin else "cli"
        prompt, metrics = prompt_builder.build(
            agent,
            history,
            is_first_speaker=True,
            debug=True,
            primary=True,
            shared_state=shared_state,
            skip_tool_prompt=True,
            execution_mode=getattr(self.app, "execution_mode", None),
        )
        analysis_lines = [
            f"PROMPT PREVIEW: {agent}",
            f"DRIVER: {driver}",
            "TOOLS NO TEXTO: não",
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
            plugin_registry = getattr(self.app, "_plugin_registry", None)
            plugin = self.app.get_agent_plugin(target)
            if plugin is None:
                plugin = register_dynamic_plugin(target, registry=plugin_registry)
                self.show_system_message(f"Agente registrado dinamicamente: {target}")
            self.show_system_message(f"Configurando conexão para {target}")
            self.show_system_message(f"Atual: {format_connection_label(plugin.effective_connection())}")
            try:
                connection, base_name = self._configure_connection_interactively(plugin)
            except ValueError as exc:
                self.app.renderer.show_warning(str(exc))
                return True
            if base_name:
                base_plugin = _plugins.get(base_name)
                if base_plugin is not None:
                    object.__setattr__(plugin, "_base_plugin_name", base_plugin.name)
                    if base_plugin.spy_stdout_formatter is not None:
                        plugin.spy_stdout_formatter = base_plugin.spy_stdout_formatter
                    if base_plugin.runtime_rw_paths:
                        plugin.runtime_rw_paths = list(base_plugin.runtime_rw_paths)
            set_connection_override(target, connection, persist=True, registry=plugin_registry)
            active_agents = list(getattr(self.app, "active_agents", None) or [])
            selected_agents = list(getattr(self.app, "selected_agents", None) or [])
            if target not in active_agents:
                self.app.active_agents = active_agents + [target]
            if target not in selected_agents:
                self.app.selected_agents = selected_agents + [target]
            self.show_system_message(f"Conexão ativa para {target}: {format_connection_label(connection)}")
            return True

        if command == CMD_DISCONNECT or command.startswith(f"{CMD_DISCONNECT} "):
            target = command[len(CMD_DISCONNECT):].strip().lower()
            if not target:
                self.app.renderer.show_warning("Uso: /disconnect <agente>")
                return True
            plugin_registry = getattr(self.app, "_plugin_registry", None)
            if remove_connection(target, registry=plugin_registry):
                self.app.renderer.show_system(f"Conexão removida para {target}.")
            else:
                self.app.renderer.show_warning(f"Nenhuma conexão persistida encontrada para {target}.")
            return True

        if command == CMD_CLEAR:
            self.app.clear_terminal_screen()
            return True

        if command == CMD_RELOAD:
            names = reload_plugins(registry=getattr(self.app, "_plugin_registry", None))
            self.app.active_agents = names
            self.app.selected_agents = names
            self.app.renderer.show_system(f"Plugins recarregados: {len(names)} agentes disponíveis")
            return True

        if command == CMD_PROMPT or command.startswith(f"{CMD_PROMPT} "):
            target = self._resolve_prompt_target(command)
            if target is None:
                self.app.renderer.show_warning("Uso: /prompt [agente]")
                return True
            self.show_muted_message(self._build_prompt_preview_message(target))
            return True

        if command.startswith(CMD_TASK):
            self.app.task_services.handle_task_command(command)
            return True

        if command == CMD_RESET_STATE:
            self.app.reset_shared_state()
            self.app.renderer.show_system("shared_state limpo.")
            return True

        if command == CMD_APPROVE_ALL:
            approval_handler = getattr(self.app, "_approval_handler", None)
            if approval_handler is not None and hasattr(approval_handler, "set_approve_all"):
                approval_handler.set_approve_all(True)
                self.app.renderer.show_system("[aprovação] modo approve-all ativado — todas as ferramentas serão aprovadas automaticamente.")
            else:
                self.app.renderer.show_warning("[aprovação] mecanismo de aprovação não disponível.")
            return True

        if command == CMD_APPROVE:
            approval_handler = getattr(self.app, "_approval_handler", None)
            if approval_handler is not None and hasattr(approval_handler, "pre_approve"):
                approval_handler.pre_approve()
                self.app.renderer.show_system("[aprovação] próxima ferramenta será pré-aprovada.")
            else:
                self.app.renderer.show_warning("[aprovação] mecanismo de aprovação não disponível.")
            return True

        if command == CMD_CONTEXT:
            self.app.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            self.app.context_manager.edit()
            return True

        if command == CMD_CONTEXT_BRANCH or command.startswith(f"{CMD_CONTEXT_BRANCH} "):
            return self.app.context_manager.handle_context_branch(command)

        return False
