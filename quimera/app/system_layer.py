"""Componentes de `quimera.app.system_layer`."""
from __future__ import annotations

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
    CMD_CONTEXT_BRANCH,
    CMD_CONTEXT_EDIT,
    CMD_DISCONNECT,
    CMD_CONTEXT,
    CMD_HELP,
    CMD_POLICY,
    CMD_PROMPT,
    CMD_RELOAD,
    CMD_RESET,
    CMD_TASK,
    DEFAULT_FIRST_AGENT,
    build_agents_help,
    build_help,
)
from ..connection_configurator import ConnectionConfigurator
from ..profiles import remove_connection
from .. import profiles as _profiles
from ..profiles.base import (
    CliConnection,
    OpenAIConnection,
    format_connection_label,
    get_connections,
    is_valid_agent_name,
    register_connection_profile,
    reload_profiles,
    set_connection,
)


class _NullProfileResolver:
    def get(self, name: str):
        return None

    @property
    def profiles(self) -> list:
        return []


class AppSystemLayer:
    """Encapsula comandos de sistema e delega display para ``DisplayService``."""

    def __init__(
        self,
        agent_pool: IAgentPool,
        renderer=None,
        profile_resolver=None,
        prompt_builder=None,
        history_getter=None,
        shared_state_getter=None,
        execution_mode_getter=None,
        get_selected_agents=None,
        set_selected_agents=None,
        clear_screen=None,
        input_status_getter=None,
        redisplay_prompt=None,
        output_lock=None,
        prompt_owner_thread_id_getter=None,
        run_above_active_prompt=None,
        read_user_input=None,
        task_command_handler=None,
        bugs_command_handler=None,
        session_state_manager=None,
        approval_handler_getter=None,
        context_manager=None,
        profile_registry=None,
        deferred_messages_getter=None,
        max_deferred_messages_getter=None,
        workspace_policy_getter=None,
        workspace_policy_setter=None,
        display_service=None,
    ):
        """Inicializa uma instância de AppSystemLayer."""
        if display_service is not None:
            self._display = display_service
        else:
            self._display = DisplayService(
                renderer=renderer,
                input_status_getter=input_status_getter,
                redisplay_prompt=redisplay_prompt,
                output_lock=output_lock,
                prompt_owner_thread_id_getter=prompt_owner_thread_id_getter,
                run_above_active_prompt=run_above_active_prompt,
                deferred_messages_getter=deferred_messages_getter,
                max_deferred_messages_getter=max_deferred_messages_getter,
            )

        self.profile_resolver = profile_resolver or _NullProfileResolver()
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
        self.session_state_manager = session_state_manager
        self.approval_handler_getter = approval_handler_getter or (lambda: None)
        self.context_manager = context_manager
        self.profile_registry = profile_registry
        self._deferred_system_messages: list[tuple[str, str]] = []
        self._deferred_messages_getter = deferred_messages_getter
        self._max_deferred_messages_getter = max_deferred_messages_getter
        self.workspace_policy_getter = workspace_policy_getter
        self.workspace_policy_setter = workspace_policy_setter

    @property
    def _display(self):
        try:
            return self.__display
        except AttributeError:
            raise RuntimeError("DisplayService não inicializado")

    @_display.setter
    def _display(self, value):
        self.__display = value

    def _get_renderer(self):
        return self._display._get_renderer()

    def _get_active_agents(self) -> list[str]:
        return self.agent_pool.agents

    def _current_workspace_policy_name(self) -> str:
        """Retorna o preset de policy atual para exibição."""
        if callable(self.workspace_policy_getter):
            try:
                name = str(self.workspace_policy_getter() or "").strip().lower()
            except Exception:
                name = ""
            if name in {"strict", "autonomous"}:
                return name
        return "strict"

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

        profile = self.profile_resolver.get(agent)
        driver = profile.effective_driver() if profile else "cli"
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
        block_lines = ["ANÁLISE DOS BLOCOS:"]
        for block in prompt.blocks:
            label = f'"{block.title}"' if block.title else f"<{block.name}>"
            block_lines.append(f"- {block.name} {label}: {block.size} chars")
        block_lines += [
            f"- history_messages: {metrics['history_messages']}",
            f"- total_chars: {metrics['total_chars']}",
        ]
        analysis_lines += block_lines + [
            "",
            "PROMPT FINAL:",
            prompt,
        ]
        return "\n".join(analysis_lines)

    def _configure_connection_interactively(self, profile, *, advanced: bool = True):
        """Coleta configuração de conexão de forma interativa no chat.

        Retorna (connection, profile_name | None).
        """
        configurator = ConnectionConfigurator(
            self._prompt_text,
            self._prompt_bool,
            self._display.show_warning_message,
            get_profile=_profiles.get,
        )
        return configurator.configure_with_profile(profile, advanced=advanced)

    def _resolve_prompt_target(self, command: str) -> str | None:
        """Resolve o agente alvo para preview de prompt."""
        raw_target = command[len(CMD_PROMPT):].strip()

        # Aceita prefixo 'show ' para robustez e compatibilidade com confusão comum
        if raw_target.lower().startswith("show "):
            raw_target = raw_target[len("show "):].strip()
        elif raw_target.lower() == "show":
            raw_target = ""

        active_agents = self._get_active_agents()

        if not raw_target:
            if not active_agents:
                return None
            if len(active_agents) > 1:
                # Solicita o agente interativamente como solicitado pelo usuário
                choices = ", ".join(active_agents)
                agent_name = self._prompt_text(f"Escolha o agente para o preview ({choices})", active_agents[0])
                if not agent_name:
                    return None
                raw_target = agent_name.strip()
            else:
                if DEFAULT_FIRST_AGENT in active_agents:
                    return DEFAULT_FIRST_AGENT
                return active_agents[0]

        normalized = raw_target.lower()
        if normalized.startswith("/"):
            normalized = normalized[1:]

        for agent_name in active_agents:
            if normalized == agent_name.lower():
                return agent_name
            profile = self.profile_resolver.get(agent_name)
            if profile is None:
                continue
            candidates = {profile.prefix.lower().lstrip("/")}
            candidates.update(alias.lower().lstrip("/") for alias in (getattr(profile, "aliases", None) or []))
            if normalized in candidates:
                return agent_name
        return None

    def _resolve_connect_target(self, command: str) -> str | None:
        """Resolve o agente alvo para configuração de conexão."""
        raw_target = command[len(CMD_CONNECT):].strip().lower()
        if not raw_target:
            return None

        normalized = raw_target[1:] if raw_target.startswith("/") else raw_target
        for profile in getattr(self.profile_resolver, "profiles", []):
            if normalized == profile.name.lower():
                return profile.name
            candidates = {profile.prefix.lower().lstrip("/")}
            candidates.update(alias.lower().lstrip("/") for alias in (getattr(profile, "aliases", None) or []))
            if normalized in candidates:
                return profile.name
        return normalized if is_valid_agent_name(normalized) else None

    def list_connected_agents(self) -> list[str]:
        """Retorna nomes dos agentes com conexão persistida."""
        return sorted(get_connections().keys())

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

    def notify_agent_retry(
        self,
        agent: str,
        reason: str,
        attempt: int,
        limit: int,
        detail: str = "",
    ) -> None:
        """Encaminha nova tentativa estruturada de agente para o display."""
        self._display.notify_agent_retry(agent, reason, attempt, limit, detail)

    def notify_agent_failover(self, agent: str, target: str) -> None:
        """Encaminha failover estruturado entre agentes para o display."""
        self._display.notify_agent_failover(agent, target)

    def show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Exibe task response."""
        text = response.strip()
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
            advanced = " --advanced" in f" {command}" or " --full" in f" {command}"
            command_for_target = command.replace(" --advanced", "").replace(" --full", "")
            target = self._resolve_connect_target(command_for_target)
            if target is None:
                self._display.show_warning_message("Uso: /connect <agente> [--advanced]")
                return True
            profile_registry = self.profile_registry
            profile = self.profile_resolver.get(target)
            if profile is None:
                profile = register_connection_profile(target, registry=profile_registry)
                self.show_system_message(f"Conexão registrada: {target}")
            self.show_system_message(f"Configurando conexão para {target}")
            self.show_system_message(f"Atual: {format_connection_label(profile.effective_connection())}")
            try:
                connection, profile_name = self._configure_connection_interactively(profile, advanced=advanced)
            except ValueError as exc:
                self._display.show_warning_message(str(exc))
                return True
            if profile_name:
                profile = register_connection_profile(
                    target,
                    metadata={"profile": profile_name},
                    registry=profile_registry,
                )
            set_connection(target, connection, persist=True, registry=profile_registry)
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
            profile_registry = self.profile_registry
            if remove_connection(target, registry=profile_registry):
                self._display.show_system(f"Conexão removida para {target}.")
            else:
                self._display.show_warning_message(f"Nenhuma conexão persistida encontrada para {target}.")
            return True

        if command == CMD_CLEAR:
            self.clear_screen()
            return True

        if command == CMD_RELOAD:
            current_agents = list(self.agent_pool.agents)
            current_selected_agents = list(self.get_selected_agents() or [])
            all_names = reload_profiles(registry=self.profile_registry)
            surviving = [a for a in current_agents if a in all_names]
            surviving_selected = [a for a in current_selected_agents if a in surviving]
            self.agent_pool.set(surviving)
            self.set_selected_agents(surviving_selected)
            self._display.show_system(f"Profiles recarregados: {len(all_names)} profile(s)")
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

        if command == CMD_RESET or command.startswith(f"{CMD_RESET} "):
            target = command[len(CMD_RESET):].strip() or "state"
            session_state_manager = self.session_state_manager
            if session_state_manager is not None and hasattr(session_state_manager, "reset"):
                msg = session_state_manager.reset(target)
            else:
                msg = "reset não disponível."
            self._display.show_system(msg)
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

        if command == CMD_POLICY or command.startswith(f"{CMD_POLICY} "):
            raw_target = command[len(CMD_POLICY):].strip().lower()
            if raw_target in {"", "status", "show"}:
                current = self._current_workspace_policy_name()
                self._display.show_system(
                    f"[policy] atual: {current}. Opções: strict, autonomous."
                )
                return True
            if raw_target not in {"strict", "autonomous"}:
                self._display.show_warning_message(
                    "Uso: /policy [status|strict|autonomous]"
                )
                return True
            if callable(self.workspace_policy_setter):
                self.workspace_policy_setter(raw_target)
                self._display.show_system(f"[policy] workspace_policy={raw_target}")
            else:
                self._display.show_warning_message("[policy] configuração indisponível.")
            return True

        if command == CMD_CONTEXT_EDIT or command.startswith(f"{CMD_CONTEXT_EDIT} "):
            if self.context_manager is not None:
                self.context_manager.edit()
            return True

        if command == CMD_CONTEXT_BRANCH or command.startswith(f"{CMD_CONTEXT_BRANCH} "):
            if self.context_manager is not None:
                self.context_manager.handle_context_branch(command)
            return True

        if command == CMD_CONTEXT or command.startswith(f"{CMD_CONTEXT} "):
            if self.context_manager is None:
                return True
            parts = command[len(CMD_CONTEXT):].strip().split()
            sub = parts[0] if parts else None
            if sub is None or sub == "show":
                self.context_manager.show()
            elif sub == "edit":
                self.context_manager.edit()
            elif sub == "branch":
                self.context_manager.handle_context_branch(command)
            else:
                self._display.show_warning_message(
                    "Uso: /context [show|edit|branch [nome]]"
                )
            return True

        if command.startswith("s/") and len(command) > 2:
            parts = command[2:].strip().split(None, 1)
            agent = parts[0]
            trailing = parts[1].strip() if len(parts) > 1 else ""
            try:
                self.agent_pool.freeze(agent)
            except ValueError:
                self._display.show_warning_message(
                    f"Agente '{agent}' não está no pool ativo."
                )
                return True
            self._display.show_system(
                f"[rotação] congelada para {agent} — "
                "todo input não-prefixado irá para este agente."
            )
            if trailing:
                return trailing
            return True

        if command.startswith("o/") and len(command) > 2:
            parts = command[2:].strip().split(None, 1)
            agent = parts[0]
            trailing = parts[1].strip() if len(parts) > 1 else ""
            others = [a for a in self.agent_pool.agents if a != agent]
            if not others:
                self._display.show_warning_message(
                    f"Modo orquestrador requer pelo menos um outro agente ativo além de '{agent}'."
                )
                return True
            try:
                self.agent_pool.set_orchestrator(agent)
            except ValueError:
                self._display.show_warning_message(
                    f"Agente '{agent}' não está no pool ativo."
                )
                return True
            self._display.show_system(
                f"[orquestrador] {agent} ativado — todo input passa por ele antes de delegar."
                f" Agentes disponíveis: {', '.join(others)}."
            )
            if trailing:
                return trailing
            return True

        if command.startswith("r/") and len(command) >= 2:
            was_orchestrator = getattr(self.agent_pool, "orchestrator_agent", None)
            self.agent_pool.unfreeze()
            if was_orchestrator:
                self._display.show_system(
                    f"[orquestrador] {was_orchestrator} desativado — agentes voltam a rotacionar."
                )
            else:
                self._display.show_system(
                    "[rotação] descongelada — agentes voltam a rotacionar."
                )
            return True

        return False
