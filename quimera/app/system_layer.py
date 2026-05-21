"""Componentes de `quimera.app.system_layer`."""
from __future__ import annotations
import json
import re
import shlex
import threading
from contextlib import nullcontext

from .display_service import DisplayService
from .interfaces import IAgentPool

from ..constants import (
    CMD_AGENTS,
    CMD_ALIASES,
    CMD_APPROVE,
    CMD_APPROVE_ALL,
    CMD_BUGS,
    CMD_CLEAR,
    CMD_CONNECT,
    CMD_DISCONNECT,
    CMD_CONTEXT,
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


class _NullPluginResolver:
    def get(self, name: str):
        return None

    @property
    def plugins(self) -> list:
        return []


class _LegacyPluginResolver:
    def __init__(self, app):
        self._app = app

    def get(self, name: str):
        getter = getattr(self._app, "get_agent_plugin", None)
        if callable(getter):
            return getter(name)
        return None

    @property
    def plugins(self) -> list:
        getter = getattr(self._app, "get_available_plugins", None)
        if callable(getter):
            return list(getter())
        return []


class _LegacyAgentPoolAdapter:
    """Adapter mínimo para o contrato de agent pool em call sites legados.

    Mantém leitura dinâmica de ``app.active_agents`` para preservar o
    comportamento esperado pelos testes e por inicializações antigas
    de ``AppSystemLayer(app)``.
    """

    def __init__(self, app):
        self._app = app

    @property
    def agents(self) -> list[str]:
        return list(getattr(self._app, "active_agents", []) or [])

    def add(self, name: str) -> None:
        agents = self.agents
        if name not in agents:
            agents.append(name)
            setattr(self._app, "active_agents", agents)

    def set(self, agents: list[str]) -> None:
        setattr(self._app, "active_agents", list(agents))

    def __contains__(self, name: str) -> bool:
        return name in self.agents


class AppSystemLayer:
    """Encapsula comandos de sistema e delega display para ``DisplayService``."""

    def __init__(
        self,
        agent_pool: IAgentPool,
        renderer=None,
        plugin_resolver=None,
        prompt_builder=None,
        history_getter=None,
        shared_state_getter=None,
        execution_mode_getter=None,
        get_selected_agents=None,
        set_selected_agents=None,
        clear_screen=None,
        input_status_getter=None,
        clear_prompt_line=None,
        redisplay_prompt=None,
        output_lock=None,
        prompt_owner_thread_id_getter=None,
        run_above_active_prompt=None,
        read_user_input=None,
        task_command_handler=None,
        bugs_command_handler=None,
        reset_shared_state=None,
        approval_handler_getter=None,
        context_manager=None,
        plugin_registry=None,
        deferred_messages_getter=None,
        max_deferred_messages_getter=None,
        display_service=None,
    ):
        """Inicializa uma instância de AppSystemLayer."""
        if display_service is not None:
            self._display = display_service
        else:
            if renderer is None and not hasattr(agent_pool, "agents"):
                renderer = agent_pool
            looks_like_renderer = hasattr(renderer, "show_system")
            if renderer is not None and not looks_like_renderer:
                app = renderer
                renderer = lambda: getattr(app, "renderer", None)
                ensure_agent_pool = getattr(app, "_ensure_agent_pool", None)
                if callable(ensure_agent_pool):
                    agent_pool = ensure_agent_pool()
                else:
                    agent_pool = getattr(app, "agent_pool", None)
                    if agent_pool is None or not hasattr(agent_pool, "agents"):
                        agent_pool = _LegacyAgentPoolAdapter(app)
                plugin_resolver = plugin_resolver or _LegacyPluginResolver(app)
                prompt_builder = prompt_builder or getattr(app, "prompt_builder", None)
                history_getter = history_getter or (lambda: list(getattr(app, "history", []) or []))
                shared_state_getter = shared_state_getter or (lambda: getattr(app, "shared_state", None))
                execution_mode_getter = execution_mode_getter or (lambda: getattr(app, "execution_mode", None))
                get_selected_agents = get_selected_agents or (
                    lambda: list(getattr(app, "selected_agents", []) or [])
                )
                set_selected_agents = set_selected_agents or (
                    lambda agents: setattr(app, "selected_agents", list(agents))
                )
                clear_screen = clear_screen or (lambda: getattr(app, "clear_terminal_screen", lambda: None)())
                input_status_getter = input_status_getter or (
                    lambda: getattr(app, "_nonblocking_input_status", "idle")
                )
                clear_prompt_line = clear_prompt_line or getattr(
                    app, "_clear_user_prompt_line_if_needed", None
                )
                redisplay_prompt = redisplay_prompt or getattr(
                    app, "_redisplay_user_prompt_if_needed", None
                )
                output_lock = output_lock or (lambda: getattr(app, "_output_lock", nullcontext()))
                prompt_owner_thread_id_getter = prompt_owner_thread_id_getter or (
                    lambda: getattr(app, "_prompt_owning_thread_id", None)
                )
                if run_above_active_prompt is None:
                    input_gate = getattr(app, "input_gate", None)
                    run_above_active_prompt = getattr(input_gate, "run_in_terminal_message", None)
                read_user_input = read_user_input or getattr(app, "read_user_input", None)
                if task_command_handler is None:
                    task_services = getattr(app, "task_services", None)
                    task_command_handler = getattr(task_services, "handle_task_command", None)
                bugs_command_handler = bugs_command_handler or getattr(app, "_handle_bugs_command", None)
                reset_shared_state = reset_shared_state or getattr(app, "reset_shared_state", None)
                approval_handler_getter = approval_handler_getter or (
                    lambda: getattr(app, "_approval_handler", None)
                )
                context_manager = context_manager or getattr(app, "context_manager", None)
                plugin_registry = plugin_registry or getattr(app, "_plugin_registry", None)
                deferred_messages_getter = deferred_messages_getter or (
                    lambda: getattr(app, "_deferred_system_messages", [])
                )
                max_deferred_messages_getter = max_deferred_messages_getter or (
                    lambda: getattr(app, "_MAX_DEFERRED_SYSTEM_MESSAGES", 20)
                )

            self._display = DisplayService(
                renderer=renderer,
                input_status_getter=input_status_getter,
                clear_prompt_line=clear_prompt_line,
                redisplay_prompt=redisplay_prompt,
                output_lock=output_lock,
                prompt_owner_thread_id_getter=prompt_owner_thread_id_getter,
                run_above_active_prompt=run_above_active_prompt,
                deferred_messages_getter=deferred_messages_getter,
                max_deferred_messages_getter=max_deferred_messages_getter,
            )

        self.plugin_resolver = plugin_resolver or _NullPluginResolver()
        self._prompt_builder = prompt_builder
        self._history_getter = history_getter or (lambda: [])
        self._shared_state_getter = shared_state_getter or (lambda: None)
        self._execution_mode_getter = execution_mode_getter or (lambda: None)
        self.agent_pool = agent_pool
        self.get_selected_agents = get_selected_agents or (lambda: [])
        self.set_selected_agents = set_selected_agents or (lambda _agents: None)
        self.clear_screen = clear_screen or (lambda: None)
        self.prompt_owner_thread_id_getter = (
            prompt_owner_thread_id_getter or (lambda: None)
        )
        self.run_above_active_prompt = run_above_active_prompt
        self.read_user_input = read_user_input
        self.task_command_handler = task_command_handler
        self.bugs_command_handler = bugs_command_handler
        self.reset_shared_state = reset_shared_state
        self.approval_handler_getter = approval_handler_getter or (lambda: None)
        self.context_manager = context_manager
        self.plugin_registry = plugin_registry
        self._deferred_system_messages: list[tuple[str, str]] = []
        self._deferred_messages_getter = deferred_messages_getter
        self._max_deferred_messages_getter = max_deferred_messages_getter

    @property
    def _display(self):
        try:
            return object.__getattribute__(self, '_AppSystemLayer__display')
        except AttributeError:
            raise RuntimeError("DisplayService não inicializado")

    @_display.setter
    def _display(self, value):
        object.__setattr__(self, '_AppSystemLayer__display', value)

    def _get_renderer(self):
        return self._display._get_renderer()

    def _get_active_agents(self) -> list[str]:
        return self.agent_pool.agents

    def _read_command_input(self, prompt: str) -> str | None:
        """Lê input síncrono para comandos interativos do chat."""
        if callable(self.read_user_input):
            return self.read_user_input(prompt, timeout=-1)
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
            self._display.show_warning_message("Valor inválido. Use 's' ou 'n'.")

    # ------------------------------------------------------------------
    # Delegados estáticos para compatibilidade com testes (lógica em DisplayService)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_terminal_task_message(message: str) -> bool:
        return DisplayService._is_terminal_task_message(message)

    @staticmethod
    def _extract_task_id(message: str) -> int | None:
        return DisplayService._extract_task_id(message)

    @staticmethod
    def _format_task_summary(task_id: int, message: str, retry_count: int = 0) -> str:
        return DisplayService._format_task_summary(task_id, message, retry_count)

    @staticmethod
    def _compact_deferred(deferred: list) -> list:
        return DisplayService._compact_deferred(deferred)

    @staticmethod
    def _dedup_without_terminal(deferred: list) -> list:
        return DisplayService._dedup_without_terminal(deferred)

    def _build_prompt_preview_message(self, agent: str, is_first_speaker: bool = True) -> str:
        """Monta a saída textual do comando /prompt."""
        history = list(self._history_getter() or [])
        shared_state = self._shared_state_getter()
        prompt_builder = self._prompt_builder
        if prompt_builder is None:
            raise RuntimeError("prompt_builder indisponível")

        plugin = self.plugin_resolver.get(agent)
        driver = plugin.effective_driver() if plugin else "cli"
        prompt, metrics = prompt_builder.build(
            agent,
            history,
            is_first_speaker=is_first_speaker,
            debug=True,
            primary=True,
            shared_state=shared_state,
            skip_tool_prompt=True,
            execution_mode=self._execution_mode_getter(),
        )

        mode_label = "primeiro-falante" if is_first_speaker else "follower/reviewer"
        persistent_chars = metrics.get("persistent_chars", 0)
        persistent_notice = (
            f"AVISO: histórico parcialmente sumarizado ({persistent_chars} chars em persistent_context)"
            if persistent_chars else ""
        )
        analysis_lines = [
            f"PROMPT PREVIEW: {agent}",
            f"MODO: {mode_label}",
            f"DRIVER: {driver}",
            "TOOLS NO TEXTO: não",
        ]
        if persistent_notice:
            analysis_lines.append(persistent_notice)
        analysis_lines += [
            "ANÁLISE DOS BLOCOS:",
            f"- regras_chars: {metrics['rules_chars']}",
            f"- session_state_chars: {metrics['session_state_chars']}",
            f"- persistent_chars: {persistent_chars}",
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
            self._display.show_warning_message("Driver inválido. Use 'cli' ou 'openai'.")
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
                if extra_body == {}:
                    extra_body = None
            except json.JSONDecodeError as exc:
                self._display.show_warning_message(f"JSON inválido: {exc}. extra_body será ignorado.")
                extra_body = api_defaults.extra_body
        else:
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

    def _resolve_prompt_target(self, command: str) -> str | None:
        """Resolve o agente alvo para preview de prompt."""
        raw_target = command[len(CMD_PROMPT):].strip().lower()
        active_agents = self._get_active_agents()

        if not raw_target:
            if DEFAULT_FIRST_AGENT in active_agents:
                return DEFAULT_FIRST_AGENT
            return active_agents[0] if active_agents else None

        normalized = raw_target[1:] if raw_target.startswith("/") else raw_target
        for agent_name in active_agents:
            if normalized == agent_name.lower():
                return agent_name
            plugin = self.plugin_resolver.get(agent_name)
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
        for plugin in getattr(self.plugin_resolver, "plugins", []):
            if normalized == plugin.name.lower():
                return plugin.name
            candidates = {plugin.prefix.lower().lstrip("/")}
            candidates.update(alias.lower().lstrip("/") for alias in (getattr(plugin, "aliases", None) or []))
            if normalized in candidates:
                return plugin.name
        return normalized if is_valid_agent_name(normalized) else None

    def list_connected_agents(self) -> list[str]:
        """Retorna nomes dos agentes com conexão persistida."""
        return sorted(get_connection_overrides().keys())

    # ------------------------------------------------------------------
    # Delegação para DisplayService
    # ------------------------------------------------------------------

    def show_system_message(self, message: str) -> None:
        self._display.show_system_message(message)

    def show_muted_message(self, message: str) -> None:
        self._display.show_muted_message(message)

    def show_warning_message(self, message: str) -> None:
        self._display.show_warning_message(message)

    def show_error_message(self, message: str) -> None:
        self._display.show_error_message(message)

    def show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Exibe task response."""
        text = strip_tool_block(response).strip()
        if text:
            self._display.show_muted_message(f"[task {task_id}] {agent}:\n{text}")

    def flush_deferred_messages(self) -> None:
        self._display.flush_deferred_messages()

    def _enqueue_deferred_message(self, message: str, level: str = "system") -> bool:
        return self._display._enqueue_deferred_message(message, level=level)

    def handle_command(self, user_input: str) -> bool:
        """Processa command."""
        command = user_input.strip()
        command = CMD_ALIASES.get(command, command)
        renderer = self._display._get_renderer()
        if renderer is None:
            return False

        if command == CMD_HELP:
            renderer.show_system(build_help(self._get_active_agents()))
            return True

        if command == CMD_AGENTS:
            renderer.show_system(build_agents_help(self._get_active_agents()))
            return True

        if command == CMD_BUGS or command.startswith(f"{CMD_BUGS} "):
            if callable(self.bugs_command_handler):
                return bool(self.bugs_command_handler(command))
            self._display.show_warning_message("Comando /bugs indisponível nesta sessão.")
            return True

        if command == CMD_CONNECT or command.startswith(f"{CMD_CONNECT} "):
            target = self._resolve_connect_target(command)
            if target is None:
                self._display.show_warning_message("Uso: /connect <agente>")
                return True
            plugin_registry = self.plugin_registry
            plugin = self.plugin_resolver.get(target)
            if plugin is None:
                plugin = register_dynamic_plugin(target, registry=plugin_registry)
                self.show_system_message(f"Agente registrado dinamicamente: {target}")
            self.show_system_message(f"Configurando conexão para {target}")
            self.show_system_message(f"Atual: {format_connection_label(plugin.effective_connection())}")
            try:
                connection, base_name = self._configure_connection_interactively(plugin)
            except ValueError as exc:
                self._display.show_warning_message(str(exc))
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
            active_agents = self._get_active_agents()
            selected_agents = list(self.get_selected_agents() or [])
            if target not in self.agent_pool:
                self.agent_pool.add(target)
            if target not in selected_agents:
                self.set_selected_agents(selected_agents + [target])
            self.show_system_message(f"Conexão ativa para {target}: {format_connection_label(connection)}")
            return True

        if command == CMD_DISCONNECT or command.startswith(f"{CMD_DISCONNECT} "):
            target = command[len(CMD_DISCONNECT):].strip().lower()
            if not target:
                self._display.show_warning_message("Uso: /disconnect <agente>")
                return True
            plugin_registry = self.plugin_registry
            if remove_connection(target, registry=plugin_registry):
                self._display.show_system(f"Conexão removida para {target}.")
            else:
                self._display.show_warning_message(f"Nenhuma conexão persistida encontrada para {target}.")
            return True

        if command == CMD_CLEAR:
            self.clear_screen()
            return True

        if command == CMD_RELOAD:
            names = reload_plugins(registry=self.plugin_registry)
            self.agent_pool.set(names)
            self.set_selected_agents(names)
            self._display.show_system(f"Plugins recarregados: {len(names)} agentes disponíveis")
            return True

        if command == CMD_PROMPT or command.startswith(f"{CMD_PROMPT} "):
            raw_args = command[len(CMD_PROMPT):].strip()
            is_follower = raw_args.endswith(" follower") or raw_args == "follower"
            if is_follower:
                raw_args = raw_args[: -len("follower")].rstrip()
            lookup_cmd = f"{CMD_PROMPT} {raw_args}".rstrip() if raw_args else CMD_PROMPT
            target = self._resolve_prompt_target(lookup_cmd)
            if target is None:
                self._display.show_warning_message("Uso: /prompt [agente] [follower]")
                return True
            preview = self._build_prompt_preview_message(target, is_first_speaker=not is_follower)
            self._display.show_prompt_preview(target, preview)
            return True

        if command.startswith(CMD_TASK):
            if callable(self.task_command_handler):
                self.task_command_handler(command)
            return True

        if command == CMD_RESET_STATE:
            if callable(self.reset_shared_state):
                self.reset_shared_state()
            self._display.show_system("shared_state limpo.")
            return True

        if command == CMD_APPROVE_ALL:
            approval_handler = self.approval_handler_getter()
            if approval_handler is not None and hasattr(approval_handler, "set_approve_all"):
                approval_handler.set_approve_all(True)
                self._display.show_system("[aprovação] modo approve-all ativado — todas as ferramentas serão aprovadas automaticamente.")
            else:
                self._display.show_warning_message("[aprovação] mecanismo de aprovação não disponível.")
            return True

        if command == CMD_APPROVE:
            approval_handler = self.approval_handler_getter()
            if approval_handler is not None and hasattr(approval_handler, "pre_approve"):
                approval_handler.pre_approve()
                self._display.show_system("[aprovação] próxima ferramenta será pré-aprovada.")
            else:
                self._display.show_warning_message("[aprovação] mecanismo de aprovação não disponível.")
            return True

        if command == CMD_CONTEXT:
            if self.context_manager is not None:
                self.context_manager.show()
            return True

        if command == CMD_CONTEXT_EDIT:
            if self.context_manager is not None:
                self.context_manager.edit()
            return True

        if command == CMD_CONTEXT_BRANCH or command.startswith(f"{CMD_CONTEXT_BRANCH} "):
            return bool(self.context_manager and self.context_manager.handle_context_branch(command))

        return False
