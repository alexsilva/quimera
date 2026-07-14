"""Fachada compatível de `QuimeraApp`.

Os métodos aqui preservam a API legada enquanto `core.py` permanece como
construtor fino. A implementação foi movida mecanicamente na Fase 5.
"""

import os
from pathlib import Path

from .agent_pool import AgentPoolView
from .bootstrap.wiring import normalize_agent_name
from .chat_processor import run_chat_loop
from .inputs import AskUserPrompter
from .lifecycle import AppLifecycle
from .prompt_formatter import PromptFormatter
from .state import ExecutionModeState
from .turn import TurnManager
from .worker import ChatWorker
from .. import profiles
from ..bugs import BugEvidenceRef, BugReport
from ..constants import (
    CMD_AGENTS, CMD_ALIASES, CMD_BUGS, CMD_CLEAR, CMD_CONNECT, CMD_DISCONNECT, CMD_CONTEXT, CMD_EDIT, CMD_EXIT,
    CMD_APPROVE, CMD_APPROVE_ALL, CMD_FILE_PREFIX, CMD_HELP,
    CMD_POLICY, CMD_PROMPT, CMD_RELOAD, CMD_RESET, CMD_TASK,
    MSG_SESSION_LOG,
)
from ..modes import MODES
from ..runtime.workspace_policy import WorkspacePolicy
from ..tasks.classifiers import classify_task_execution_result, parse_task_command


def _core_sys():
    from . import core as core_module
    return core_module.sys


class CoreFacadeMixin:
    """Métodos de fachada herdados por `QuimeraApp`."""
    _SESSION_LOG_DISPLAY_MAX_CHARS = 96

    # ── bound-method helpers used by wiring (replace lambdas) ──────────

    def get_selected_agents(self) -> list[str]:
        return list(self.selected_agents or [])

    def set_selected_agents(self, agents: list[str]) -> None:
        self.selected_agents = list(agents)

    def get_approval_handler(self):
        return self.__dict__.get("_approval_handler")

    def resolve_input_gate(self):
        return self.input_gate

    def is_debug_prompt_enabled(self) -> bool:
        return bool(self.debug_prompt_metrics)

    def get_tool_executor(self):
        return self.__dict__.get("tool_executor")

    def get_dispatch_services(self):
        return self.__dict__.get("dispatch_services")

    def get_dispatch_tool_executor(self):
        return self.__dict__.get("tool_executor")

    def set_approval_handler(self, handler):
        self._approval_handler = handler

    def get_workspace_policy_ref(self):
        return self.__dict__.get("workspace_policy")

    def get_session_services_ref(self):
        return self.__dict__.get("session_services")

    def get_agent_client_ref(self):
        return self.__dict__.get("agent_client")

    def get_history_ref(self):
        return self.__dict__.get("history", [])

    def get_session_started_at_ref(self):
        return self.__dict__.get("_session_started_at", 0.0)

    # ──────────────────────────────────────────────────────────────────

    @property
    def active_agents(self):
        """Retorna uma visão em lista dos agentes ativos do pool da sessão."""
        return AgentPoolView(self.agent_pool)

    @active_agents.setter
    def active_agents(self, agents) -> None:
        self.agent_pool.set(list(agents or []))

    @property
    def summary_agent_preference(self):
        """Retorna o agente preferido para sumarização."""
        chat_state = self.__dict__.get("_chat_state")
        if chat_state is not None:
            return chat_state.summary_agent_preference
        return self.__dict__.get("_summary_agent_preference_fallback")

    @summary_agent_preference.setter
    def summary_agent_preference(self, value):
        chat_state = self.__dict__.get("_chat_state")
        if chat_state is not None:
            chat_state.summary_agent_preference = value
        else:
            self.__dict__["_summary_agent_preference_fallback"] = value

    @staticmethod
    def _available_internal_commands() -> list[str]:
        """Retorna os comandos internos e aliases aceitos pela aplicação."""
        commands = {
            CMD_AGENTS,
            CMD_APPROVE,
            CMD_APPROVE_ALL,
            CMD_BUGS,
            CMD_CLEAR,
            CMD_CONNECT,
            CMD_DISCONNECT,
            CMD_CONTEXT,
            CMD_EDIT,
            CMD_EXIT,
            CMD_FILE_PREFIX,
            CMD_HELP,
            CMD_POLICY,
            CMD_PROMPT,
            CMD_RELOAD,
            CMD_RESET,
            CMD_TASK,
            *CMD_ALIASES,
            *MODES.keys(),
        }
        return sorted(commands)

    @staticmethod
    def _format_yes_no(value):
        """Formata yes no."""
        return "sim" if value else "não"

    def _available_commands(self) -> list[str]:
        """Retorna todos os comandos disponíveis para autocomplete."""
        commands = set(self._available_internal_commands())
        for agent_name in self.agent_pool:
            profile = self._profile_resolver.get(agent_name)
            if profile and profile.prefix:
                commands.add(profile.prefix)
        return sorted(commands)

    def _command_argument_resolver(self, command: str, partial: str) -> list[str]:
        """Resolve sugestões de argumentos para comandos com autocomplete contextual."""
        if command == CMD_CONTEXT:
            return ["show", "edit", "branch"]
        if command == CMD_PROMPT:
            return sorted(self.agent_pool)
        if command == CMD_DISCONNECT:
            return self.system_layer.list_connected_agents()
        if command == CMD_BUGS:
            return ["list", "show", "close", "analyze", "stats"]
        if command == CMD_POLICY:
            return ["status", "strict", "autonomous"]
        if command == CMD_RESET:
            return ["state", "history", "all"]
        if command in ("s", "o", "r"):
            return sorted(self.agent_pool)
        return []

    def _resolve_profile_style(self, agent: str):
        """Resolve (color, label) para o agente; retorna None se não encontrado."""
        profile = self._profile_resolver.get(agent)
        return profile.render_style if profile else None

    def configure_mcp_socket(self, socket_path: str | None, token: str | None = None) -> None:
        """Propaga socket MCP e token para os profiles dos agentes ativos."""
        resolver = self.__dict__.get("_profile_resolver")
        agent_pool = self.__dict__.get("agent_pool")
        if resolver is not None and agent_pool is not None:
            resolver.configure_mcp_socket(agent_pool, socket_path, token)
            return
        for profile in self.get_active_agent_profiles():
            config_setter = getattr(profile, "set_mcp_socket_config", None)
            if callable(config_setter):
                config_setter(socket_path, token)
            else:
                path_setter = getattr(profile, "set_mcp_socket_path", None)
                if callable(path_setter):
                    path_setter(socket_path)

    def configure_mcp_http(self, url: str | None, token: str | None = None) -> None:
        """Propaga endpoint MCP HTTP e token para os profiles dos agentes ativos."""
        resolver = self.__dict__.get("_profile_resolver")
        agent_pool = self.__dict__.get("agent_pool")
        if resolver is not None and agent_pool is not None:
            resolver.configure_mcp_http(agent_pool, url, token)
            return
        for profile in self.get_active_agent_profiles():
            config_setter = getattr(profile, "set_mcp_http_config", None)
            if callable(config_setter):
                config_setter(url, token)

    def get_agent_profile(self, agent):
        """Retorna o profile associado ao agente, ou None."""
        resolver = self.__dict__.get("_profile_resolver")
        if resolver is not None:
            return resolver.get(agent)
        return profiles.get(agent)

    def get_available_profiles(self) -> list:
        """Retorna todos os profiles disponíveis."""
        resolver = self.__dict__.get("_profile_resolver")
        if resolver is not None:
            return resolver.profiles
        return profiles.all_profiles()

    def get_active_agent_profiles(self) -> list:
        """Retorna profiles dos agentes ativos no pool."""
        resolver = self.__dict__.get("_profile_resolver")
        if resolver is not None:
            return resolver.active_profiles(self.agent_pool)
        agent_pool = self.__dict__.get("agent_pool")
        if agent_pool is None:
            return []
        return [p for name in (agent_pool.agents or []) if (p := profiles.get(name)) is not None]

    def delegate(self, agent, **options):
        """Delega uma mensagem para o agente especificado."""
        return self.dispatch_services.delegate(agent, **options)

    def _refresh_parallel_toolbar(self) -> None:
        """Solicita redraw do prompt de paralelismo."""
        coordinator = self.__dict__.get("toolbar_coordinator")
        if coordinator is not None:
            coordinator.refresh()

    def _get_parallel_toolbar_state(self) -> dict:
        """Retorna cópia do estado de paralelismo da toolbar."""
        coordinator = self.__dict__.get("toolbar_coordinator")
        if coordinator is not None:
            return coordinator.get_parallel_toolbar_state()
        toolbar = self.__dict__.get("toolbar")
        if toolbar is not None:
            return toolbar._get_parallel_toolbar_state()
        return {}

    def _set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents=None,
    ) -> None:
        """Atualiza o estado de paralelismo na toolbar."""
        coordinator = self.__dict__.get("toolbar_coordinator")
        if coordinator is not None:
            coordinator.set_parallel_toolbar_state(
                active=active,
                queued=queued,
                capacity=capacity,
                active_agents=active_agents,
            )

    def _resolve_active_model_label(self) -> str:
        """Resolve o modelo ativo para exibição na toolbar."""
        coordinator = self.__dict__.get("toolbar_coordinator")
        if coordinator is not None:
            return coordinator.resolve_active_model_label()
        return "unknown"

    def _resolve_next_responder_label(self) -> str:
        """Resolve o agente que responde na próxima rodada."""
        coordinator = self.__dict__.get("toolbar_coordinator")
        if coordinator is not None:
            return coordinator.resolve_next_responder_label()
        return "unknown"

    def _build_input_toolbar_context(self) -> dict:
        """Retorna contexto da toolbar do input."""
        coordinator = self.__dict__.get("toolbar_coordinator")
        if coordinator is not None:
            return coordinator.build_input_toolbar_context()
        return {}

    def _has_mcp_pending(self) -> bool:
        """Retorna True enquanto o MCP server interno tem tool calls em execução.

        Usado pelo ProcessRunner para suspender o idle timer do agente enquanto
        ele aguarda silenciosamente a resposta de uma tool call longa (ex: delegate).
        ``internal_mcp_server`` é inicializado explicitamente como ``None`` no
        construtor e atualizado por ``start_embedded_mcp``.
        """
        server = self.__dict__.get("internal_mcp_server")
        return bool(server and server.has_pending_calls)

    def record_success(self, agent):
        """Reseta o contador de falhas de um agente após resposta bem-sucedida."""
        tracker = self.__dict__.get("failure_tracker")
        if tracker is not None:
            tracker.record_success(agent)

    def record_failure(self, agent):
        """Registra failure e aplica política de remoção via AgentFailureTracker."""
        tracker = self.__dict__.get("failure_tracker")
        if tracker is not None:
            tracker.record_failure(agent)

    def _file_bug(
        self,
        *,
        session_id: str,
        category: str,
        summary: str,
        severity: str = "medium",
        confidence: float = 0.5,
        description: str = "",
        agent: str = "",
        evidence_refs: list[BugEvidenceRef] | None = None,
    ) -> BugReport | None:
        return self.bug_services.file_bug(
            session_id=session_id,
            category=category,
            summary=summary,
            severity=severity,
            confidence=confidence,
            description=description,
            agent=agent,
            evidence_refs=evidence_refs,
        )

    def _run_render_bug_detector(self) -> None:
        session_state = self.__dict__.get("session_state", {}) or {}
        agent_metrics = session_state.get("agent_metrics", {})
        self.bug_services.run_render_bug_detector(agent_metrics=agent_metrics)

    @staticmethod
    def _unique_encodings(*encodings):
        """Executa unique encodings."""
        seen = set()
        result = []
        for encoding in encodings:
            if not encoding:
                continue
            normalized = str(encoding).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
        return result

    @staticmethod
    def _shorten_middle(value: str, max_chars: int) -> str:
        """Trunca string no meio para manter cabeçalho e sufixo visíveis."""
        if max_chars <= 0 or len(value) <= max_chars:
            return value
        if max_chars <= 7:
            return value[:max_chars]
        head_len = (max_chars - 3) // 2
        tail_len = max_chars - 3 - head_len
        return f"{value[:head_len]}...{value[-tail_len:]}"

    def _format_session_log_message(self, log_file: str | Path) -> str:
        """Monta mensagem de log com path compactado para evitar quebra feia no terminal."""
        path_text = str(log_file)
        home_dir = str(Path.home())
        home_prefix = f"{home_dir}{os.sep}"
        if path_text.startswith(home_prefix):
            path_text = f"~{path_text[len(home_dir):]}"
        path_text = self._shorten_middle(path_text, self._SESSION_LOG_DISPLAY_MAX_CHARS)
        return MSG_SESSION_LOG.format(path_text)

    def _do_process_chat_message(self, user: str) -> None:
        """Executa processamento de uma mensagem de chat via ChatLifecycle."""
        self.chat_lifecycle._do_process_message(user)

    def _wire_event_ui(self) -> None:
        """Conecta eventos de domínio à renderização UI."""
        self._ui_subscriptions = self._ui_event_handler.wire_event_ui()

    def _claim_gate(self) -> bool:
        """Gate de reivindicação de tasks em modo single-thread.

        Só retorna True se o turn_manager não existe ou se é turno do humano.
        """
        tm = self.__dict__.get("turn_manager")
        return not (tm is not None and not tm.is_human_turn)

    def _setup_task_executors(self):
        """Set up task executors for explicit human-created task execution."""
        claim_gate = None
        if int(self.__dict__.get("threads", 1) or 1) <= 1:
            claim_gate = self._claim_gate
        self.task_services.setup_task_executors(claim_gate=claim_gate)

    def _stop_task_executors(self):
        """Executa stop task executors."""
        self.task_services.stop_task_executors()

    def _make_ask_user_fn(self):
        """Cria callable de ask_user usando o prompter de entrada."""
        return AskUserPrompter(self.input_gate, self.renderer).ask

    def _cleanup_sub_agent_stream(self, agent_name: str) -> None:
        """Limpa o estado de render do agente chamado via delegate.

        Remove o stream transitório do sub-agente do Live display e da
        rolling buffer, evitando vazamento de estado em _stream_states
        e _active_stream_agents.
        """
        renderer = self.renderer
        if not renderer:
            return
        renderer.clear_agent_transient(agent_name)
        renderer.abort_message_stream(agent_name)

    def _redisplay_user_prompt_if_needed(self, clear_first: bool = True) -> None:
        """Executa redisplay user prompt if needed."""
        _ = clear_first  # Mantido por compatibilidade de assinatura.
        stdin = _core_sys().stdin
        if stdin is None or not stdin.isatty():
            return
        input_gate = self.__dict__.get("input_gate")
        if input_gate is None:
            return
        try:
            if not bool(input_gate.is_active()):
                return
        except Exception:
            return
        redisplay = getattr(input_gate, "redisplay", None)
        if callable(redisplay):
            try:
                redisplay()
            except Exception:
                pass

    def clear_terminal_screen(self) -> None:
        """Limpa a viewport e o scrollback do terminal, reposicionando o cursor."""
        renderer = self.__dict__.get("renderer")
        if renderer is not None:
            renderer.clear_screen()
            return
        stdout = _core_sys().stdout
        if stdout is None or not stdout.isatty():
            return
        stdout.write("\x1b[3J\x1b[2J\x1b[H")
        stdout.flush()


    @staticmethod
    def parse_task_command(command: str) -> str:
        """Interpreta task command."""
        return parse_task_command(command)

    @staticmethod
    def classify_task_execution_result(response: str | None) -> tuple[bool, str]:
        """Return whether the task execution can be considered completed."""
        return classify_task_execution_result(response)

    def get_workspace_policy_name(self) -> str:
        """Retorna o preset de autonomia ativo no workspace."""
        return self.workspace_policy_name

    def set_workspace_policy_name(self, name: str) -> str:
        """Define, persiste e propaga o preset de autonomia do workspace."""
        normalized = WorkspacePolicy.normalize_name(name)
        self.workspace_policy_name = normalized
        self.workspace_policy = WorkspacePolicy.from_name(normalized)
        setter = getattr(self.config, "set_workspace_policy", None)
        if callable(setter):
            setter(normalized)
        self._apply_workspace_policy_to_tool_executor(self.__dict__.get("tool_executor"))
        return normalized

    def _apply_workspace_policy_to_tool_executor(self, executor) -> None:
        """Propaga policy para o executor e approval handler associados."""
        if executor is None:
            return
        config = getattr(executor, "config", None)
        if config is not None:
            config.workspace_policy = self.workspace_policy
        approval = getattr(executor, "approval_manager", None)
        approval_config = getattr(approval, "config", None)
        if approval_config is not None:
            approval_config.workspace_policy = self.workspace_policy

    @property
    def execution_mode(self) -> object | None:
        return self.execution_mode_state.get()

    @execution_mode.setter
    def execution_mode(self, mode: object | None) -> None:
        self.execution_mode_state.set(mode)

    @property
    def execution_mode_state(self) -> ExecutionModeState:
        # Lazy: testes instanciam QuimeraApp via __new__ (sem __init__) e
        # acessam execution_mode diretamente.
        state = self.__dict__.get("_execution_mode_state")
        if state is None:
            state = self._create_execution_mode_state()
            self._execution_mode_state = state
        return state

    def _create_execution_mode_state(self) -> ExecutionModeState:
        # A propagação para agent_client/policy é comportamento do app, não
        # de montagem: o listener nasce junto com o estado para valer também
        # em construções parciais (testes via __new__).
        state = ExecutionModeState()
        state.on_change(self._on_execution_mode_changed)
        return state

    def _on_execution_mode_changed(self, old: object | None, new: object | None) -> None:
        _ = old  # unused but part of listener protocol
        agent_client = self.__dict__.get("agent_client")
        if agent_client is not None:
            agent_client.execution_mode = new
        tool_executor = self.__dict__.get("tool_executor")
        if tool_executor is not None and new is not None:
            tool_executor.policy.blocked_tools = list(new.blocked_tools)
        elif tool_executor is not None:
            tool_executor.policy.blocked_tools = []

    def _set_execution_mode(self, mode):
        """Define o modo de execução ativo (delega ao state)."""
        self.execution_mode = mode

    def parse_routing(self, user_input: str):
        """Analisa o input do usuário e identifica o agente destino e modo de roteamento."""
        return self.command_router.parse_routing(user_input)

    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1

    def _record_tool_event(self, agent, result=None, loop_abort=False, reason=None):
        """Registra métricas de uso de ferramentas atribuídas ao agente."""
        ok, is_invalid, error_type = self.session_metrics.classify_tool_event_result(result)
        self.session_metrics.record_tool_event(
            self,
            agent,
            ok=ok,
            is_invalid=is_invalid,
            loop_abort=loop_abort,
            reason=reason,
            error_type=error_type,
        )

    def resolve_agent_response(
            self,
            agent: str,
            response: str | None,
            silent: bool = False,
            persist_history: bool = True,
            show_output: bool = True,
    ) -> str | None:
        """Fachada compatível para resolução de respostas com tools."""
        return self.dispatch_services.resolve_agent_response(
            agent,
            response,
            silent=silent,
            persist_history=persist_history,
            show_output=show_output,
        )

    def print_response(self, agent, response):
        """Fachada compatível para renderização de respostas."""
        return self.dispatch_services.print_response(agent, response)

    def _format_user_prompt(self) -> str:
        """Retorna o prompt visível ao humano com nome e modo atual."""
        mode = self.execution_mode
        active_mode = getattr(mode, "name", None) if mode is not None else None
        return PromptFormatter.format_user_prompt(self.user_name, active_mode)

    def read_user_input(self, prompt, timeout: int):
        """Fachada compatível para leitura de input."""
        input_services = self.__dict__.get("input_services")
        if input_services is None:
            return None
        return input_services.read_user_input(prompt, timeout)

    def _handle_bugs_command(self, command: str) -> bool:
        return self.bug_services.handle_bugs_command(
            command,
            app_session_state=self.__dict__.get("session_state")
        )

    def handle_command(self, user_input: str) -> bool:
        """Fachada compatível para comandos slash."""
        return self.system_layer.handle_command(user_input)

    def parse_response(self, response, **_kwargs):
        """Analisa a resposta estruturada do agente e aplica mutations ao estado compartilhado."""
        protocol = self.protocol
        if getattr(protocol, "_shared_state", None) is not self.shared_state:
            sync_shared_state = getattr(protocol, "set_shared_state", None)
            if callable(sync_shared_state):
                sync_shared_state(self.shared_state)
        return self.protocol.parse_response(response)

    def _restore_current_job_env(self) -> None:
        """Restaura QUIMERA_CURRENT_JOB_ID para evitar vazamento entre sessões."""
        previous = self.__dict__.get("_previous_current_job_id_env")
        if previous is None:
            os.environ.pop("QUIMERA_CURRENT_JOB_ID", None)
        else:
            os.environ["QUIMERA_CURRENT_JOB_ID"] = previous

    def _should_render_ui_event_above_prompt(self) -> bool:
        """Retorna True quando há prompt ativo controlado por outra thread."""
        return self._ui_event_handler._should_render_ui_event_above_prompt()

    def _run_ui_event_above_prompt(self, callback) -> bool:
        """Tenta renderizar callback acima do prompt ativo via InputGate."""
        return self._ui_event_handler._run_ui_event_above_prompt(callback)

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        run_chat_loop(
            self,
            chat_worker_cls=ChatWorker,
            turn_manager_cls=TurnManager,
        )

    def close(self, *, interrupted: bool = False) -> None:
        """Fecha explicitamente os recursos da aplicação."""
        lifecycle = self.__dict__.get("lifecycle")
        if lifecycle is None:
            lifecycle = AppLifecycle(self)
            self.lifecycle = lifecycle
        lifecycle.close(interrupted=interrupted)
