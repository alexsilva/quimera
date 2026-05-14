"""Componentes de `quimera.app.core`."""
import os
import platform
import queue
import random
import shutil
import sys
import threading
import time
from collections import defaultdict
from contextlib import nullcontext
from importlib import metadata
from pathlib import Path

from .handlers import PromptAwareStderrHandler
from .chat_round import ChatRoundOrchestrator
from .protocol import AppProtocol
from .session import AppSessionServices, compute_history_hard_limit, trim_history_messages
from .session_metrics import SessionMetricsService
from .dispatch import AppDispatchServices
from .inputs import AppInputServices
from .prompt_input import InputGate
from .task import AppTaskServices, create_executor
from .task_classifiers import classify_task_execution_result, parse_task_command
from .system_layer import AppSystemLayer
from .turn import TurnManager
from .event_sink import EventSink
from .task_events import (
    TaskStarted,
    TaskCompleted,
    TaskFailed,
    TaskProposed,
    TaskSubmittedForReview,
    TaskRequeued,
)
from .task_utils import summarize_task_feedback
from .. import plugins
from ..plugins.base import PluginRegistry, extract_model_from_cli_cmd
from ..runtime.parser import strip_tool_block
from ..runtime import tasks as runtime_tasks
from ..ui import TerminalRenderer
from ..context import ContextManager
from ..storage import SessionStorage
from ..agents import AgentClient
from ..session_summary import SessionSummarizer, build_chain_summarizer
from ..prompt import PromptBuilder
from ..workspace import Workspace
from ..config import ConfigManager, DEFAULT_USER_NAME
from ..metrics import BehaviorMetricsTracker
from ..constants import (
    CMD_AGENTS, CMD_ALIASES, CMD_CLEAR, CMD_CONNECT, CMD_DISCONNECT, CMD_CONTEXT, CMD_CONTEXT_BRANCH, CMD_CONTEXT_EDIT, CMD_EDIT, CMD_EXIT,
    CMD_APPROVE, CMD_APPROVE_ALL, CMD_FILE_PREFIX, CMD_HELP,
    CMD_PROMPT, CMD_RESET_STATE, CMD_TASK,
    MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_SHUTDOWN, MSG_DOUBLE_PREFIX,
    Visibility,
)
from ..modes import MODES, get_mode
from .config import logger


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    _SESSION_LOG_DISPLAY_MAX_CHARS = 96

    def __init__(self,
                 cwd: Path,
                 debug: bool = False,
                 history_window: int | None = None,
                 agents: list | None = None,
                 threads: int = 1,
                 timeout: int | None = None,
                 idle_timeout_seconds: int | None = None,
                 visibility: Visibility = Visibility.SUMMARY,
                 theme: str | None = None,
                 workspace: Workspace | None = None,
                 auto_approve_mutations: bool = False,
                 plugin_registry: PluginRegistry | None = None,
                 ):
        """Inicializa uma instância de QuimeraApp."""
        self.selected_agents = list(agents) if agents else []
        self.active_agents = self.selected_agents
        self.threads = int(threads) if threads is not None else 1
        self.agent_failures = defaultdict(int)
        self._agent_failures_lock = threading.Lock()
        self.workspace = workspace if workspace is not None else Workspace(cwd)
        self.auto_approve_mutations = auto_approve_mutations
        self._plugin_registry = plugin_registry
        self.config = ConfigManager(self.workspace.config_file)
        _active_theme = theme if theme is not None else self.config.theme
        self.renderer = TerminalRenderer(theme=_active_theme, get_plugin_style=self._resolve_plugin_style, density=self.config.density)
        self.event_sink = EventSink()
        self._wire_event_ui()
        self.user_name = self.config.user_name
        self.visibility = Visibility(visibility)
        self.system_layer = AppSystemLayer(self)
        self.protocol = AppProtocol(self, decisions_log_path=self.workspace.decisions_log)
        self.session_metrics = SessionMetricsService()
        self.task_services = AppTaskServices(self)
        self.dispatch_services = AppDispatchServices(self)
        self.session_services = AppSessionServices(self)
        self.history_file = self.workspace.history_file
        self.input_gate = InputGate(
            renderer=self.renderer,
            history_file=self.history_file,
            command_resolver=self._available_commands,
            argument_resolver=self._command_argument_resolver,
        )
        self.input_services = AppInputServices(
            self,
            input_resolver=lambda: self.input_gate,
        )
        self.input_gate.set_toolbar_context_resolver(self._build_input_toolbar_context)
        self.input_gate.set_theme_cycle_handler(self._cycle_renderer_theme)
        self.renderer.set_prompt_integration(
            is_active_fn=self.input_gate.is_active,
            run_above_fn=self.input_gate.run_in_terminal_message,
        )
        self.chat_round_orchestrator = ChatRoundOrchestrator(self)

        migrated = self.workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = ContextManager(
            self.workspace.context_persistent,
            self.workspace.context_session,
            self.renderer,
            workspace=self.workspace,
        )
        self.storage = SessionStorage(self.workspace.logs_dir, self.renderer)
        session_id = self.storage.get_history_file().stem
        metrics_file = self.workspace.metrics_dir / f"{session_id}.jsonl" if debug else None
        self.agent_client = AgentClient(
            self.renderer,
            metrics_file=metrics_file,
            timeout=timeout,
            visibility=self.visibility,
            working_dir=str(self.workspace.cwd),
            error_reporter=self.show_error_message,
            muted_reporter=self.show_muted_message,
        )
        self.task_executor_factory = create_executor
        self.session_summarizer = SessionSummarizer(
            self.renderer,
            summarizer_call=build_chain_summarizer(
                self.agent_client,
                list(dict.fromkeys(["ollama-granite4"] + (self.active_agents or []))),
            ),
        )
        self.summary_agent_preference = "ollama-granite4"
        self._pending_input_for: str | None = None
        configured_history_window = history_window or self.config.history_window
        configured_auto_summarize_threshold = self.config.auto_summarize_threshold
        history_hard_limit = compute_history_hard_limit(
            configured_history_window,
            configured_auto_summarize_threshold,
        )
        last_session = self.storage.load_last_session()
        self.history, restored_drop_count = trim_history_messages(
            last_session["messages"],
            history_hard_limit,
        )
        if restored_drop_count:
            self.renderer.show_system(
                f"[memória] histórico restaurado truncado para {len(self.history)} mensagens recentes\n"
            )
        session_context = self.context_manager.load_session()
        history_restored = bool(self.history)
        summary_loaded = self.context_manager.SUMMARY_MARKER in session_context
        self.session_state = {
            "session_id": session_id,
            "history_count": len(self.history),
            "history_restored": history_restored,
            "summary_loaded": summary_loaded,
            "handoffs_sent": 0,
            "handoffs_received": 0,
            "handoffs_succeeded": 0,
            "handoffs_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
            "rounds_without_progress": 0,
            "consecutive_redundant_responses": 0,
            "handoff_invalid_count": 0,
            "responses_with_clear_next_step": 0,
            "total_responses": 0,
        }
        # Persist metrics state to workspace so agents can resume with previous metrics
        metrics_state_path = self.workspace.state_dir / "metrics_state.json"
        self.behavior_metrics = BehaviorMetricsTracker(storage_path=metrics_state_path)
        self.agent_client.tool_event_callback = self._record_tool_event
        self.debug_prompt_metrics = debug
        self.round_index = 0
        self.session_call_index = 0
        self.shared_state = last_session["shared_state"]
        self._shared_state_lock = threading.Lock()
        self._lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._nonblocking_prompt_visible = False
        self._nonblocking_prompt_text = ""
        self._deferred_system_messages: list[str] = []
        self._MAX_DEFERRED_SYSTEM_MESSAGES = 20
        self._nonblocking_input_thread: threading.Thread | None = None
        self._nonblocking_input_queue: "queue.Queue | None" = None
        self._nonblocking_input_status = "idle"
        self._nonblocking_input_status_lock = threading.Lock()
        self._prompt_owning_thread_id: int | None = None
        self.turn_manager = TurnManager()
        for handler in logger.handlers:
            if isinstance(handler, PromptAwareStderrHandler):
                handler.bind_app(self)
        is_new_session = not history_restored and not summary_loaded

        # Unify tasks database path
        self.tasks_db_path = str(self.workspace.tasks_db)
        runtime_tasks.init_db(self.tasks_db_path)
        self.current_job_id = runtime_tasks.add_job(f"Session {session_id}", db_path=self.tasks_db_path)
        self.session_state["current_job_id"] = self.current_job_id
        os.environ["QUIMERA_CURRENT_JOB_ID"] = str(self.current_job_id)

        session_state = {
            "session_id": self.session_state["session_id"],
            "is_new_session": self._format_yes_no(is_new_session),
            "history_restored": self._format_yes_no(history_restored),
            "summary_loaded": self._format_yes_no(summary_loaded),
            "current_job_id": self.current_job_id,
            "workspace_root": str(self.workspace.cwd),
            "current_dir": ".",
            "os_info": f"{platform.system()} {platform.release()}",
        }
        self.prompt_builder = PromptBuilder(
            self.context_manager,
            history_window=configured_history_window,
            session_state=session_state,
            user_name=self.user_name,
            active_agents=self.active_agents,
            metrics_tracker=self.behavior_metrics,
        )
        self.auto_summarize_threshold = configured_auto_summarize_threshold
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds

        self.tool_executor = self.task_services.build_tool_executor(require_approval_for_mutations=not self.auto_approve_mutations)
        # Injeta o executor nos drivers de API do agent_client.
        self.agent_client.tool_executor = self.tool_executor
        # Modo de execução ativo (definido via /planning, /analysis, etc.)
        self.execution_mode = None
        # Set up task executors for autonomous task execution
        self._setup_task_executors()

    @staticmethod
    def _available_internal_commands() -> list[str]:
        """Retorna os comandos internos e aliases aceitos pela aplicação."""
        commands = {
            CMD_AGENTS,
            CMD_APPROVE,
            CMD_APPROVE_ALL,
            CMD_CLEAR,
            CMD_CONNECT,
            CMD_DISCONNECT,
            CMD_CONTEXT,
            CMD_CONTEXT_BRANCH,
            CMD_CONTEXT_EDIT,
            CMD_EDIT,
            CMD_EXIT,
            CMD_FILE_PREFIX,
            CMD_HELP,
            CMD_PROMPT,
            CMD_RESET_STATE,
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
        for plugin in self.get_available_plugins():
            if plugin.prefix:
                commands.add(plugin.prefix)
            commands.update(alias for alias in (plugin.aliases or []) if alias)
        return sorted(commands)

    def _command_argument_resolver(self, command: str, partial: str) -> list[str]:
        """Resolve sugestões de argumentos para comandos com autocomplete contextual."""
        if command == CMD_CONTEXT_BRANCH:
            return self.workspace.list_branches()
        if command == CMD_DISCONNECT:
            return self.system_layer.list_connected_agents()
        return []

    def _resolve_plugin_style(self, agent: str):
        """Resolve (color, label) para o agente; retorna None se não encontrado."""
        plugin = self.get_agent_plugin(agent)
        return plugin.render_style if plugin else None

    @staticmethod
    def _normalize_agent_name(agent):
        """Normaliza identificador de agente para nome canônico string."""
        if hasattr(agent, "name"):
            return getattr(agent, "name")
        return agent

    def get_agent_plugin(self, agent_name: str):
        """Resolve um plugin pelo nome canônico do agente."""
        normalized_name = self._normalize_agent_name(agent_name)
        if not normalized_name:
            return None
        reg = getattr(self, '_plugin_registry', None)
        if reg is not None:
            return reg.get(normalized_name)
        return plugins.get(normalized_name)

    def get_available_plugins(self) -> list:
        """Retorna a lista atual de plugins conhecidos pela aplicação."""
        reg = getattr(self, '_plugin_registry', None)
        if reg is not None:
            return list(reg.all_plugins())
        return list(plugins.all_plugins())

    def get_active_agent_plugins(self) -> list:
        """Retorna os plugins válidos dos agentes ativos na sessão."""
        active_plugins = []
        for agent_name in self.active_agents:
            plugin = self.get_agent_plugin(agent_name)
            if plugin is not None:
                active_plugins.append(plugin)
        return active_plugins

    def __del__(self):
        """Libera recursos associados à instância."""
        try:
            self._stop_task_executors()
        except Exception:
            pass

    def record_failure(self, agent):
        """Registra failure."""
        agent_name = self._normalize_agent_name(agent)
        if not agent_name:
            return
        with self._agent_failures_lock:
            self.agent_failures[agent_name] += 1
            failures = self.agent_failures[agent_name]
        if failures >= 2:
            if agent_name in self.active_agents:
                self.active_agents.remove(agent_name)
                logger.warning("agent %s removed after %d failures", agent_name, failures)
                try:
                    runtime_tasks.release_agent_tasks(agent_name, db_path=self.tasks_db_path)
                except Exception:
                    pass
        session_metrics = getattr(self, "session_metrics", None)
        if session_metrics is not None:
            session_metrics.record_agent_metric(self, agent_name, "failed", 0)

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

    def _wire_event_ui(self) -> None:
        """Conecta eventos de domínio à renderização UI."""
        def _on_task_started(event):
            self.show_muted_message(f"[task {event.task_id}] {event.assigned_to}: iniciando")

        def _on_task_completed(event):
            line = f"[task {event.task_id}] concluída"
            if event.reviewed_by:
                line = f"{line} | aprovada por {event.reviewed_by}"
            summary = summarize_task_feedback(event.result)
            if summary:
                line = f"{line}: {summary}"
            self.show_muted_message(line)

        def _on_task_failed(event):
            system_layer = getattr(self, "system_layer", None)
            if system_layer is not None and hasattr(system_layer, "show_warning_message"):
                system_layer.show_warning_message(f"[task {event.task_id}] falhou: {event.reason or 'sem motivo'}")
            else:
                self.renderer.show_warning(f"[task {event.task_id}] falhou: {event.reason or 'sem motivo'}")

        def _on_task_proposed(event):
            self.show_system_message(f"[task {event.task_id}] proposta: {event.description[:60]}")

        def _on_task_submitted(event):
            self.show_muted_message(f"[task {event.task_id}] submetida para revisão")

        def _on_task_requeued(event):
            system_layer = getattr(self, "system_layer", None)
            if system_layer is not None and hasattr(system_layer, "show_warning_message"):
                system_layer.show_warning_message(f"[task {event.task_id}] requeue (tentativa {event.attempt})")
            else:
                self.renderer.show_warning(f"[task {event.task_id}] requeue (tentativa {event.attempt})")

        self._ui_subscriptions = [
            self.event_sink.subscribe(TaskStarted, _on_task_started),
            self.event_sink.subscribe(TaskCompleted, _on_task_completed),
            self.event_sink.subscribe(TaskFailed, _on_task_failed),
            self.event_sink.subscribe(TaskProposed, _on_task_proposed),
            self.event_sink.subscribe(TaskSubmittedForReview, _on_task_submitted),
            self.event_sink.subscribe(TaskRequeued, _on_task_requeued),
        ]

    def _setup_task_executors(self):
        """Set up task executors for explicit human-created task execution."""
        self.task_services.setup_task_executors()

    def _stop_task_executors(self):
        """Executa stop task executors."""
        self.task_services.stop_task_executors()

    def _redisplay_user_prompt_if_needed(self, clear_first: bool = True) -> None:
        """Executa redisplay user prompt if needed."""
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        status_lock = getattr(self, "_nonblocking_input_status_lock", nullcontext())
        with status_lock:
            if self._nonblocking_input_status != "reading":
                return
        try:
            prompt = getattr(self, "_nonblocking_prompt_text", "")
            line_buffer = ""
            input_gate = getattr(self, "input_gate", None)
            if input_gate is not None and hasattr(input_gate, "get_line_buffer"):
                try:
                    line_buffer = input_gate.get_line_buffer()
                except Exception:
                    line_buffer = ""
            full_line = f"{prompt}{line_buffer}"
            if len(full_line) > 0:
                if clear_first:
                    self._clear_user_prompt_line_if_needed()
                sys.stdout.write(full_line)
                sys.stdout.flush()
                if input_gate is not None and hasattr(input_gate, "redisplay"):
                    try:
                        input_gate.redisplay()
                    except Exception:
                        pass
        except Exception:
            pass

    def _clear_user_prompt_line_if_needed(self) -> None:
        """Executa clear user prompt line if needed."""
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        status_lock = getattr(self, "_nonblocking_input_status_lock", nullcontext())
        with status_lock:
            if self._nonblocking_input_status != "reading":
                return
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def clear_terminal_screen(self) -> None:
        """Limpa a viewport e o scrollback do terminal, reposicionando o cursor."""
        stdout = sys.stdout
        if stdout is None or not stdout.isatty():
            return
        self._clear_user_prompt_line_if_needed()
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

    def _set_execution_mode(self, mode):
        """Define o modo de execução ativo e propaga para policy e agent_client."""
        self.execution_mode = mode
        self.agent_client.execution_mode = mode
        if mode is not None:
            self.tool_executor.policy.blocked_tools = list(mode.blocked_tools)
        else:
            self.tool_executor.policy.blocked_tools = []

    def parse_routing(self, user_input):
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada.

        Detecta comandos de modo (/planning, /analysis, etc.) e os aplica antes
        do roteamento normal. Retorna (agent, message, explicit) onde explicit=True
        indica que o usuário usou /claude ou /codex explicitamente.
        """
        stripped = user_input.lstrip()
        lowered = stripped.lower()

        # Detecta comandos de modo: /planning, /analysis, /design, /review, /execute
        first_token = lowered.split()[0] if lowered.split() else ""
        mode = get_mode(first_token)
        if mode is not None:
            self._set_execution_mode(mode)
            rest = stripped[len(first_token):].lstrip()
            mode_message = (
                f"[modo] {mode.name} ativado — restrições anteriores removidas; "
                "ferramentas bloqueadas: nenhuma"
                if mode.name == "execute"
                else f"[modo] {mode.name} ativado — ferramentas bloqueadas: "
                     f"{', '.join(mode.blocked_tools) or 'nenhuma'}"
            )
            if rest:
                self.renderer.show_system(mode_message)
                return self.parse_routing(rest)
            self.renderer.show_system(mode_message)
            if not self.active_agents:
                self.active_agents = [self._normalize_agent_name(a) for a in self.selected_agents]
            return None, "", False

        active_plugins = self.get_active_agent_plugins()
        for p in active_plugins:
            prefixes = [p.prefix, *(getattr(p, "aliases", None) or [])]
            agent = p.name
            for prefix in prefixes:
                if lowered == prefix:
                    return agent, "", True
                if lowered.startswith(f"{prefix} "):
                    message = stripped[len(prefix):].lstrip()
                    lowered_message = message.lower()
                    other_prefixes = []
                    for op in active_plugins:
                        if op.name == agent:
                            continue
                        other_prefixes.extend([op.prefix, *(getattr(op, "aliases", None) or [])])
                    if any(lowered_message == op or lowered_message.startswith(f"{op} ") for op in other_prefixes):
                        self.renderer.show_warning(MSG_DOUBLE_PREFIX)
                        return None, None, False
                    return agent, message, True

        if not self.active_agents:
            logger.warning("no active agents, resetting to default")
            logger.warning("DEBUG selected_agents=%r", self.selected_agents)
            logger.warning("DEBUG available=%r", self.get_available_plugins())
            self.active_agents = self.selected_agents or [p.name for p in self.get_available_plugins()]
            logger.warning("DEBUG after fallback active_agents=%r", self.active_agents)
            if not self.active_agents:
                raise RuntimeError("No agents available")
        return self.active_agents[0], user_input, False

    @staticmethod
    def _merge_state_value(current, incoming):
        """Mescla state value."""
        return AppProtocol.merge_state_value(current, incoming)

    def _apply_state_update(self, block_content):
        """Executa apply state update."""
        return self.protocol.apply_state_update(block_content)

    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1

    def _record_tool_event(self, agent, result=None, loop_abort=False, reason=None):
        """Registra métricas de uso de ferramentas atribuídas ao agente."""
        error_type = getattr(result, "error_type", None) if result is not None else None
        if not isinstance(error_type, str) or not error_type:
            lowered_error = str(getattr(result, "error", "") or "").lower()
            if any(
                marker in lowered_error
                for marker in (
                    "sem política para a ferramenta",
                    "bloqueada pelo modo de execução",
                    "comando bloqueado",
                    "comando inválido",
                    "comando fora da allowlist",
                    "path fora da workspace",
                )
            ):
                error_type = "policy"
            elif lowered_error:
                error_type = "generic"
            else:
                error_type = "none"
        is_invalid = error_type == "policy"
        ok = bool(getattr(result, "ok", False))
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

    def call_agent(self, agent, **options):
        """Fachada compatível para despacho de agentes."""
        if hasattr(self, "_call_agent"):
            dispatch_options = dict(options)
            silent = dispatch_options.pop("silent", False)
            persist_history = dispatch_options.pop("persist_history", True)
            show_output = dispatch_options.pop("show_output", True)
            response = self._call_agent(agent, silent=silent, **dispatch_options)
            return self.resolve_agent_response(
                agent,
                response,
                silent=silent,
                persist_history=persist_history,
                show_output=show_output,
            )
        return self.dispatch_services.call_agent(agent, **options)

    def print_response(self, agent, response):
        """Fachada compatível para renderização de respostas."""
        return self.dispatch_services.print_response(agent, response)

    @staticmethod
    def _format_user_prompt(user_name: str | None, mode_name: str | None = None) -> str:
        """Formata prompt humano, exibindo `[mode]` apenas fora do modo default."""
        normalized_name = str(user_name or "").strip()
        if not normalized_name:
            normalized_name = DEFAULT_USER_NAME
        if normalized_name not in {">", ">>>"}:
            normalized_name = normalized_name.rstrip(":").rstrip(">").strip() or DEFAULT_USER_NAME

        normalized_mode = str(mode_name or "").strip().lower() or "default"
        if normalized_mode in {"default", "execute"}:
            if normalized_name in {">", ">>>"}:
                return f"{normalized_name} "
            return f"{normalized_name}: "
        if normalized_name in {">", ">>>"}:
            return f"{normalized_name} [{normalized_mode}]: "
        return f"{normalized_name} [{normalized_mode}]: "

    def _build_input_prompt(self) -> str:
        """Retorna o prompt visível ao humano com nome e modo atual."""
        active_mode = getattr(getattr(self, "execution_mode", None), "name", None)
        return self._format_user_prompt(self.user_name, active_mode)

    @staticmethod
    def _resolve_app_version() -> str:
        """Resolve a versão instalada do pacote, com fallback seguro."""
        try:
            return metadata.version("quimera")
        except Exception:
            return "dev"

    @staticmethod
    def _build_welcome_logo() -> str:
        """Retorna logo ASCII simples para o banner inicial."""
        return (
            " / __ \\__  __(_)___ ___  ___  _________ _\n"
            "/ / / / / / / / __ `__ \\/ _ \\/ ___/ __ `/\n"
            "/ /_/ / /_/ / / / / / / /  __/ /  / /_/ / \n"
            "\\___\\_\\__,_/_/_/ /_/ /_/\\___/_/   \\__,_/  "
        )

    def _build_welcome_message(self) -> str:
        """Monta texto de boas-vindas com versão e path do projeto."""
        version = self._resolve_app_version()
        workspace = getattr(self, "workspace", None)
        project_path = str(getattr(workspace, "cwd", Path.cwd()))
        logo_lines = self._build_welcome_logo().split("\n")
        logo_lines[-1] = logo_lines[-1].rstrip() + f"  v{version}"
        return f"{chr(10).join(logo_lines)}\n"

    def _resolve_active_model_label(self) -> str:
        """Resolve o modelo ativo a partir do primeiro plugin/agente ativo."""
        active_agents = getattr(self, "active_agents", None) or []
        agent_name = active_agents[0] if active_agents else None
        if not agent_name:
            return "unknown"
        plugin = self.get_agent_plugin(agent_name)
        if plugin is None:
            return str(agent_name)
        connection = plugin.effective_connection() if hasattr(plugin, "effective_connection") else None
        model = getattr(connection, "model", None) if connection is not None else None
        if model:
            return str(model)

        cmd = getattr(connection, "cmd", None) if connection is not None else None
        if not cmd and hasattr(plugin, "effective_cmd"):
            try:
                cmd = plugin.effective_cmd()
            except Exception:
                cmd = None
        if not cmd:
            cmd = getattr(plugin, "cmd", None)

        workspace = getattr(self, "workspace", None)
        cwd = str(getattr(workspace, "cwd", Path.cwd()))
        cli_model: str | None = None
        resolver = getattr(plugin, "resolve_runtime_model", None)
        if callable(resolver):
            try:
                resolved = resolver(cwd=cwd)
            except TypeError:
                resolved = resolver()
            if isinstance(resolved, str):
                normalized = resolved.strip()
                if normalized:
                    cli_model = normalized
        if cli_model is None:
            cli_model = extract_model_from_cli_cmd(cmd)
        if isinstance(cli_model, str) and cli_model.strip():
            return cli_model.strip()

        plugin_model = getattr(plugin, "model", None)
        return str(plugin_model) if plugin_model else str(plugin.name)

    def _resolve_next_responder_label(self) -> str:
        """Resolve o agente que deve responder na próxima rodada."""
        pending_input_for = str(getattr(self, "_pending_input_for", "") or "").strip()
        if pending_input_for:
            return pending_input_for
        active_agents = getattr(self, "active_agents", None) or []
        if active_agents:
            return str(active_agents[0])
        return "unknown"

    def _cycle_renderer_theme(self) -> None:
        """Avança para o próximo tema no TerminalRenderer e persiste na config."""
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            return
        cycle = getattr(renderer, "cycle_theme", None)
        if callable(cycle):
            new_name = cycle()
            if new_name and hasattr(self, "config"):
                self.config.set_theme(new_name)

    def _build_input_toolbar_context(self) -> dict[str, str]:
        """Retorna dados de contexto exibidos na toolbar do input."""
        workspace = getattr(self, "workspace", None)
        ctx = {
            "responder": self._resolve_next_responder_label(),
            "model": self._resolve_active_model_label(),
            "cwd": str(getattr(workspace, "cwd", Path.cwd())),
        }
        renderer = getattr(self, "renderer", None)
        theme_name = getattr(renderer, "theme_name", "") if renderer else ""
        ctx["theme"] = theme_name
        return ctx

    def read_user_input(self, prompt, timeout: int):
        """Fachada compatível para leitura de input."""
        return self.input_services.read_user_input(prompt, timeout)

    def handle_command(self, user_input: str) -> bool:
        """Fachada compatível para comandos slash."""
        return self.system_layer.handle_command(user_input)

    def show_system_message(self, message: str) -> None:
        """Fachada compatível para mensagens de sistema."""
        system_layer = getattr(self, "system_layer", None)
        if system_layer is not None:
            system_layer.show_system_message(message)

    def show_muted_message(self, message: str) -> None:
        """Fachada compatível para mensagens neutras (dim)."""
        system_layer = getattr(self, "system_layer", None)
        if system_layer is not None and hasattr(system_layer, "show_muted_message"):
            system_layer.show_muted_message(message)
            return
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            return
        show_system_neutral = getattr(renderer, "show_system_neutral", None)
        if callable(show_system_neutral):
            show_system_neutral(message)
            return
        show_system = getattr(renderer, "show_system", None)
        if callable(show_system):
            show_system(message)
            return
        show_plain = getattr(renderer, "show_plain", None)
        if callable(show_plain):
            show_plain(message)
            return

    def show_error_message(self, message: str) -> None:
        """Fachada compatível para mensagens de erro."""
        system_layer = getattr(self, "system_layer", None)
        if system_layer is not None and hasattr(system_layer, "show_error_message"):
            system_layer.show_error_message(message)
            return
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            return
        show_error = getattr(renderer, "show_error", None)
        if callable(show_error):
            show_error(message)

    def show_warning_message(self, message: str) -> None:
        """Fachada compatível para mensagens de aviso."""
        system_layer = getattr(self, "system_layer", None)
        if system_layer is not None and hasattr(system_layer, "show_warning_message"):
            system_layer.show_warning_message(message)
            return
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            return
        show_warning = getattr(renderer, "show_warning", None)
        if callable(show_warning):
            show_warning(message)
            return
        show_system = getattr(renderer, "show_system", None)
        if callable(show_system):
            show_system(message)
            return

    def _do_process_chat_message(self, user):
        """Fachada compatível para a implementação da rodada de chat."""
        self.chat_round_orchestrator.process(user)

    @staticmethod
    def _generate_handoff_id(task, target, timestamp=None):
        """Executa generate handoff id."""
        return AppProtocol.generate_handoff_id(task, target, timestamp=timestamp)

    def parse_handoff_payload(self, payload, target=None):
        """Interpreta handoff payload."""
        return self.protocol.parse_handoff_payload(payload, target=target)

    def parse_response(self, response):
        """Interpreta response."""
        return self.protocol.parse_response(response)

    def reset_shared_state(self) -> None:
        """Limpa o shared_state em memória e persiste o snapshot atualizado."""
        with self._lock:
            self.shared_state.clear()
            self.storage.save_history(self.history, shared_state=self.shared_state)

    def _merge_staging_to_workspace(self, staging_root: Path):
        """Mescla arquivos do staging para o workspace em ordem de índice."""

        if not staging_root.exists():
            logger.debug("merge: staging_root does not exist, skipping")
            return

        index_dirs = sorted(staging_root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 999)
        total_merged = 0

        for index_dir in index_dirs:
            if not index_dir.is_dir():
                continue
            for src in index_dir.rglob("*"):
                if not src.is_file():
                    continue
                rel_path = src.relative_to(index_dir)
                dest = self.workspace.cwd / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                total_merged += 1
                logger.debug("merged: %s -> %s", src, dest)

        logger.info("merge completed: %d files to %s", total_merged, self.workspace.cwd)

    def _process_chat_message(self, user):
        """Executa process chat message com controle de turno."""
        agent_client = getattr(self, "agent_client", None)
        if agent_client is not None:
            agent_client._user_cancelled = False
            cancel_event = getattr(agent_client, "_cancel_event", None)
            if cancel_event is not None:
                cancel_event.clear()
        try:
            self._do_process_chat_message(user)
        finally:
            if hasattr(self, "turn_manager") and self.turn_manager.is_ai_turn:
                self.turn_manager.next_turn()

    def _process_chat_queue(self, chat_queue: queue.Queue):
        """Executa process chat queue."""
        while True:
            user = chat_queue.get()
            try:
                if user is None:
                    return
                self._process_chat_message(user)
            finally:
                chat_queue.task_done()

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        agent_client = getattr(self, "agent_client", None)
        if agent_client:
            agent_client._user_cancelled = False
        show_banner = getattr(self.renderer, "show_banner", self.renderer.show_system)
        show_banner(self._build_welcome_message())
        _show_neutral = getattr(self.renderer, "show_system_neutral", self.renderer.show_system)
        restore_notice = getattr(self.storage, "pop_restore_notice", lambda: None)()
        if restore_notice:
            _show_neutral(restore_notice)
        _show_neutral(MSG_CHAT_STARTED)
        _show_neutral(
            MSG_SESSION_STATUS.format(
                session_id=self.session_state["session_id"],
                summary_loaded=self._format_yes_no(self.session_state["summary_loaded"]),
            )
        )
        _show_neutral(self._format_session_log_message(self.storage.get_log_file()))
        flush = getattr(self.renderer, "flush", None)
        if callable(flush):
            flush()

        threaded_chat = self.threads > 1
        chat_queue = None
        chat_worker = None
        if threaded_chat:
            chat_queue = queue.Queue()
            chat_worker = threading.Thread(
                target=self._process_chat_queue,
                args=(chat_queue,),
                daemon=True,
            )
            chat_worker.start()

        try:
            while True:
                if hasattr(self, "turn_manager") and not self.turn_manager.is_human_turn:
                    if not getattr(self, "_turn_blocked_warning_shown", False):
                        self.renderer.show_system("[Aguardando resposta do agente...]")
                        self._turn_blocked_warning_shown = True
                    self.turn_manager.wait_for_human_turn(timeout=0.01)
                    continue
                self._turn_blocked_warning_shown = False

                user = self.read_user_input(self._build_input_prompt(), timeout=0)
                if user is None:
                    if not sys.stdin.isatty():
                        break
                    continue

                if user == CMD_EXIT:
                    break

                if user.strip() == CMD_EDIT:
                    content = self.input_services.read_from_editor()
                    if not content:
                        continue
                    user = content

                elif user.strip().startswith(CMD_FILE_PREFIX):
                    path_str = user.strip()[len(CMD_FILE_PREFIX):]
                    content = self.input_services.read_from_file(path_str)
                    if not content:
                        continue
                    user = content

                if self.handle_command(user):
                    continue

                if hasattr(self, "turn_manager"):
                    self.turn_manager.next_turn()
                if chat_queue is not None:
                    chat_queue.put(user)
                else:
                    self._process_chat_message(user)
        except KeyboardInterrupt:
            agent_client = getattr(self, "agent_client", None)
            if agent_client is not None:
                agent_client._user_cancelled = True
                cancel_event = getattr(agent_client, "_cancel_event", None)
                if cancel_event is not None and hasattr(cancel_event, "set"):
                    cancel_event.set()
            self.show_muted_message(MSG_SHUTDOWN)
        finally:
            try:
                if threaded_chat and chat_queue is not None:
                    chat_queue.put(None)
                if chat_worker is not None:
                    chat_worker.join(timeout=0.5)
            except KeyboardInterrupt:
                pass
            self.session_services.shutdown()
            self.agent_client.close()
            if hasattr(self, "behavior_metrics"):
                self.behavior_metrics._flush_if_dirty()
