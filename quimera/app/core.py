"""Componentes de `quimera.app.core`."""
import logging
import os
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

from . import inputs as app_input
from .handlers import PromptAwareStderrHandler
from .protocol import AppProtocol
from .session_metrics import SessionMetricsService
from . import task as app_tasks
from .inputs import AppInputServices
from .task import AppTaskServices
from .system_layer import AppSystemLayer
from .. import plugins
from ..runtime.executor import ToolExecutor
from ..runtime.parser import strip_tool_block
from ..runtime import ToolRuntimeConfig, ConsoleApprovalHandler, create_executor
from ..runtime import tasks as runtime_tasks
from ..runtime.tools.files import set_staging_root
from ..ui import TerminalRenderer
from ..context import ContextManager
from ..storage import SessionStorage
from ..agents import AgentClient
from ..session_summary import SessionSummarizer, build_chain_summarizer
from ..prompt import PromptBuilder
from ..workspace import Workspace
from ..config import ConfigManager
from ..metrics import BehaviorMetricsTracker
from ..constants import (
    EXTEND_MARKER,
    NEEDS_INPUT_MARKER,
    ROUTE_PREFIX,
    STATE_UPDATE_START, CMD_EXIT, CMD_EDIT, CMD_FILE_PREFIX, CMD_TASK,
    USER_ROLE, MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_MEMORY_SAVING, MSG_MEMORY_FAILED, MSG_SHUTDOWN,
    MSG_DOUBLE_PREFIX, MSG_EMPTY_INPUT,
    HANDOFF_SYNTHESIS_MSG,
)
from .config import logger


class TurnManager:
    """Gerencia o turno de fala no diálogo humano ↔ agente."""
    
    def __init__(self):
        self._is_human_turn = True
        self._lock = threading.Lock()
        self._human_turn_event = threading.Event()
        self._human_turn_event.set()
    
    @property
    def is_human_turn(self) -> bool:
        with self._lock:
            return self._is_human_turn
    
    @property
    def is_ai_turn(self) -> bool:
        with self._lock:
            return not self._is_human_turn
    
    def next_turn(self) -> None:
        """Alterna o turno: humano <-> agente."""
        with self._lock:
            self._is_human_turn = not self._is_human_turn
            if self._is_human_turn:
                self._human_turn_event.set()
            else:
                self._human_turn_event.clear()
    
    def reset(self) -> None:
        """Reseta para turno do humano."""
        with self._lock:
            self._is_human_turn = True
            self._human_turn_event.set()

    def wait_for_human_turn(self, timeout: float | None = None) -> bool:
        """Aguarda até o turno humano ficar disponível."""
        return self._human_turn_event.wait(timeout=timeout)


def resolve_app_dependency(name, default):
    """Resolve app dependency."""
    package = sys.modules.get("quimera.app")
    if package is None:
        return default
    return getattr(package, name, default)


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    HANDOFF_PAYLOAD_PATTERN = re.compile(
        r"^\s*task:\s*([^\n]+?)\s*(?:(?:\n|\|\s*)context:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)expected:\s*([^\n]+?))?\s*(?:(?:\n|\|\s*)priority:\s*([^\n]+?))?\s*$",
        re.IGNORECASE,
    )
    STATE_UPDATE_PATTERN = re.compile(
        r"\[STATE_UPDATE\](.*?)\[/STATE_UPDATE\]", re.DOTALL
    )
    ROUTE_PATTERN = re.compile(r"\[ROUTE:([A-Za-z0-9_-]+)\]\s*([\s\S]+)", re.M | re.I)
    ACK_PATTERN = re.compile(r"^\s*\[ACK:([A-Za-z0-9]+)\]\s*", re.M)

    def __init__(self,
                 cwd: Path,
                 debug: bool = False,
                 history_window: int | None = None,
                 agents: list | None = None,
                 threads: int = 1,
                 timeout: int | None = None,
                 idle_timeout_seconds: int | None = None,
                 spy: bool = False,
                 theme: str | None = None,
                 ):
        """Inicializa uma instância de QuimeraApp."""
        selected_agents = list(agents) if agents else []
        renderer_cls = resolve_app_dependency("TerminalRenderer", TerminalRenderer)
        config_cls = resolve_app_dependency("ConfigManager", ConfigManager)
        workspace_cls = resolve_app_dependency("Workspace", Workspace)
        context_manager_cls = resolve_app_dependency("ContextManager", ContextManager)
        session_storage_cls = resolve_app_dependency("SessionStorage", SessionStorage)
        agent_client_cls = resolve_app_dependency("AgentClient", AgentClient)
        summarizer_cls = resolve_app_dependency("SessionSummarizer", SessionSummarizer)
        prompt_builder_cls = resolve_app_dependency("PromptBuilder", PromptBuilder)
        metrics_tracker_cls = resolve_app_dependency("BehaviorMetricsTracker", BehaviorMetricsTracker)
        runtime_readline = resolve_app_dependency("readline", readline)
        self.active_agents = self._agents = selected_agents
        self.threads = int(threads) if threads is not None else 1
        self.agent_failures = defaultdict(int)
        self._agent_failures_lock = threading.Lock()
        self.config = config_cls()
        _active_theme = theme if theme is not None else self.config.theme
        self.renderer = renderer_cls(theme=_active_theme)
        self.user_name = self.config.user_name
        self.workspace = workspace_cls(cwd)
        self.spy = spy
        self.system_layer = AppSystemLayer(self)
        self.protocol = AppProtocol(logger, decisions_log_path=self.workspace.decisions_log)
        self.session_metrics = SessionMetricsService()
        self.task_services = AppTaskServices(self)
        self.input_services = AppInputServices(
            self,
            input_resolver=lambda: resolve_app_dependency("input", input),
        )

        # Configuração do histórico persistente (readline)
        self.history_file = self.workspace.history_file
        if runtime_readline:
            if self.history_file.exists():
                try:
                    runtime_readline.read_history_file(str(self.history_file))
                except Exception:
                    pass
            runtime_readline.set_history_length(1000)

        migrated = self.workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = context_manager_cls(
            self.workspace.context_persistent,
            self.workspace.context_session,
            self.renderer,
        )
        self.storage = session_storage_cls(self.workspace.logs_dir, self.renderer)
        session_id = self.storage.get_history_file().stem
        metrics_file = self.workspace.metrics_dir / f"{session_id}.jsonl" if debug else None
        self.agent_client = agent_client_cls(
            self.renderer,
            metrics_file=metrics_file,
            timeout=timeout,
            spy=self.spy,
            working_dir=str(self.workspace.cwd),
        )
        self.task_executor_factory = resolve_app_dependency("create_executor", create_executor)
        self._create_task_executor = self.task_executor_factory
        self.session_summarizer = summarizer_cls(
            self.renderer,
            summarizer_call=build_chain_summarizer(
                self.agent_client,
                list(dict.fromkeys(["ollama-qwen"] + (self.active_agents or []))),
            ),
        )
        self.summary_agent_preference = "ollama-qwen"
        self._pending_input_for: str | None = None
        last_session = self.storage.load_last_session()
        self.history = last_session["messages"]
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
        self.behavior_metrics = metrics_tracker_cls(storage_path=metrics_state_path)
        self.agent_client.tool_event_callback = self._record_tool_event
        self.debug_prompt_metrics = debug
        self.round_index = 0
        self.session_call_index = 0
        self.shared_state = last_session["shared_state"]
        self._lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._nonblocking_prompt_visible = False
        self._nonblocking_prompt_text = ""
        self._deferred_system_messages: list[str] = []
        self._nonblocking_input_thread: threading.Thread | None = None
        self._nonblocking_input_queue: "queue.Queue | None" = None
        self._nonblocking_input_status = "idle"
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
        }
        self.prompt_builder = prompt_builder_cls(
            self.context_manager,
            history_window=history_window or self.config.history_window,
            session_state=session_state,
            user_name=self.user_name,
            active_agents=self.active_agents,
            metrics_tracker=self.behavior_metrics,
        )
        self.auto_summarize_threshold = self.config.auto_summarize_threshold
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds

        self.tool_executor = ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=self.workspace.cwd,
                db_path=Path(self.tasks_db_path) if self.tasks_db_path else None,
                require_approval_for_mutations=False,
            ),
            approval_handler=ConsoleApprovalHandler(),
        )
        # Injeta o executor nos drivers de API do agent_client.
        self.agent_client.tool_executor = self.tool_executor
        # Set up task executors for autonomous task execution
        self.turn_manager = TurnManager()
        self._setup_task_executors()

    @staticmethod
    def _format_yes_no(value):
        """Formata yes no."""
        return "sim" if value else "não"

    def _get_system_layer(self) -> AppSystemLayer:
        """Retorna system layer."""
        layer = getattr(self, "system_layer", None)
        if layer is None:
            layer = AppSystemLayer(self)
            self.system_layer = layer
        return layer

    def _get_protocol(self) -> AppProtocol:
        """Retorna protocol."""
        protocol = getattr(self, "protocol", None)
        if protocol is None:
            protocol = AppProtocol(logger)
            self.protocol = protocol
        return protocol

    def _get_session_metrics(self) -> SessionMetricsService:
        """Retorna session metrics."""
        metrics = getattr(self, "session_metrics", None)
        if metrics is None:
            metrics = SessionMetricsService()
            self.session_metrics = metrics
        return metrics

    def _get_task_services(self) -> AppTaskServices:
        """Retorna os serviços de task associados à instância."""
        services = getattr(self, "task_services", None)
        if services is None:
            services = AppTaskServices(self)
            self.task_services = services
        return services

    def _get_input_services(self) -> AppInputServices:
        """Retorna os serviços de entrada associados à instância."""
        services = getattr(self, "input_services", None)
        if services is None:
            services = AppInputServices(
                self,
                input_resolver=lambda: resolve_app_dependency("input", input),
            )
            self.input_services = services
        return services

    def __del__(self):
        """Libera recursos associados à instância."""
        try:
            self._stop_task_executors()
        except Exception:
            pass

    def record_failure(self, agent):
        """Registra failure."""
        with self._agent_failures_lock:
            self.agent_failures[agent] += 1
            failures = self.agent_failures[agent]
        if failures >= 2:
            if agent in self.active_agents:
                self.active_agents.remove(agent)
                logger.warning("agent %s removed after %d failures", agent, failures)
                try:
                    runtime_tasks.release_agent_tasks(agent, db_path=self.tasks_db_path)
                except Exception:
                    pass
        self._record_agent_metric(agent, "failed", 0)

    def _record_failure(self, agent):
        """Registra failure."""
        self.record_failure(agent)

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

    def _setup_task_executors(self):
        """Set up task executors for explicit human-created task execution."""
        self._get_task_services().setup_task_executors()

    def _stop_task_executors(self):
        """Executa stop task executors."""
        self._get_task_services().stop_task_executors()

    def build_task_overview(self) -> dict:
        """Monta task overview."""
        return self._get_task_services().build_task_overview()

    def task_context_history_window(self) -> int:
        """Executa task context history window."""
        return self._get_task_services().task_context_history_window()

    def format_task_chat_context(self) -> str:
        """Formata task chat context."""
        return self._get_task_services().format_task_chat_context()

    def build_task_body(self, description: str) -> str:
        """Monta task body."""
        return self._get_task_services().build_task_body(description)

    def refresh_task_shared_state(self) -> None:
        """Atualiza task shared state."""
        self._get_task_services().refresh_task_shared_state()

    def _redisplay_user_prompt_if_needed(self, clear_first: bool = True) -> None:
        """Executa redisplay user prompt if needed."""
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        if self._nonblocking_input_status != "reading":
            return
        try:
            prompt = getattr(self, "_nonblocking_prompt_text", "")
            line_buffer = ""
            runtime_readline = resolve_app_dependency("readline", readline)
            if runtime_readline is not None:
                try:
                    line_buffer = runtime_readline.get_line_buffer()
                except Exception:
                    line_buffer = ""
            full_line = f"{prompt}{line_buffer}"
            if len(full_line) > 0:
                if clear_first:
                    self._clear_user_prompt_line_if_needed()
                sys.stdout.write(full_line)
                sys.stdout.flush()
                if runtime_readline is not None:
                    try:
                        runtime_readline.redisplay()
                    except Exception:
                        pass
        except Exception:
            pass

    def _clear_user_prompt_line_if_needed(self) -> None:
        """Executa clear user prompt line if needed."""
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        if self._nonblocking_input_status != "reading":
            return
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def show_system_message(self, message: str) -> None:
        """Exibe system message."""
        self._get_system_layer().show_system_message(message)

    def show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Display the actual agent response for a task execution as a system message."""
        self._get_system_layer().show_task_response(task_id, agent, response)

    def resolve_agent_response(
            self,
            agent: str,
            response: str | None,
            silent: bool = False,
            persist_history: bool = True,
            show_output: bool = True,
    ) -> str | None:
        """Resolve agent response."""
        current_response = response
        max_tool_hops = 16
        tool_history = []

        for _ in range(max_tool_hops):
            if not current_response:
                return current_response

            raw_response, tool_result = self.tool_executor.maybe_execute_from_response(current_response)

            if tool_result is None:
                return current_response

            self._record_tool_event(agent, result=tool_result)

            # Truncate tool result to reduce verbosity
            tool_payload = app_tasks.truncate_payload(tool_result.to_model_payload())

            tool_history.append(
                f"Sua resposta anterior:\n{current_response.strip()}\n\n"
                f"Resultado da ferramenta:\n{tool_payload}"
            )

            visible_text = strip_tool_block(raw_response or "")
            if visible_text:
                if show_output:
                    self.print_response(agent, visible_text)
                if persist_history:
                    self.persist_message(agent, visible_text)

            followup_handoff = (
                "Histórico de ferramentas desta rodada:\n\n"
                + "\n\n---\n\n".join(tool_history)
            )

            current_response = self._call_agent(
                agent,
                handoff=followup_handoff,
                primary=False,
                protocol_mode="tool_loop",
                silent=silent,
            )

        return "Falha: limite de execuções de ferramenta atingido."

    def handle_command(self, user_input):
        """Processa command."""
        return self._get_system_layer().handle_command(user_input)

    @staticmethod
    def parse_task_command(command: str) -> str:
        """Interpreta task command."""
        return app_tasks.parse_task_command(command, CMD_TASK)

    def get_task_routing_plugins(self):
        """Retorna task routing plugins."""
        return self._get_task_services().get_task_routing_plugins()

    @staticmethod
    def classify_task_execution_result(response: str | None) -> tuple[bool, str]:
        """Return whether the task execution can be considered completed."""
        return app_tasks.classify_task_execution_result(response)

    def count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta agent open tasks."""
        return self._get_task_services().count_agent_open_tasks(agent_name)

    def choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Choose best agent for task_type, applying open-task penalty to avoid monopolies."""
        return self._get_task_services().choose_agent_with_load_balance(task_type)

    def handle_task_command(self, command: str) -> None:
        """Processa task command."""
        self._get_task_services().handle_task_command(command)

    def read_user_input(self, prompt, timeout: int) -> str | None:
        """Lê user input."""
        return self._get_input_services().read_user_input(prompt, timeout)

    def read_from_editor(self):
        """Lê from editor."""
        return self._get_input_services().read_from_editor()

    def read_from_file(self, path_str):
        """Lê from file."""
        return self._get_input_services().read_from_file(path_str)

    def parse_routing(self, user_input):
        """Extrai o agente inicial e rejeita prefixos duplicados na mesma entrada.

        Retorna (agent, message, explicit) onde explicit=True indica que o usuário
        usou /claude ou /codex explicitamente.
        """
        stripped = user_input.lstrip()
        lowered = stripped.lower()

        active_plugins = []
        for n in self.active_agents:
            plugin = plugins.get(n)
            if plugin is not None:
                active_plugins.append(plugin)
        for p in active_plugins:
            prefix, agent = p.prefix, p.name
            if lowered == prefix:
                return agent, "", True
            if lowered.startswith(f"{prefix} "):
                message = stripped[len(prefix):].lstrip()
                lowered_message = message.lower()
                other_prefixes = [op.prefix for op in active_plugins if op.prefix != prefix]
                if any(lowered_message == op or lowered_message.startswith(f"{op} ") for op in other_prefixes):
                    self.renderer.show_warning(MSG_DOUBLE_PREFIX)
                    return None, None, False
                return agent, message, True

        if not self.active_agents:
            logger.warning("no active agents, resetting to default")
            self.active_agents = self._agents
        runtime_random = resolve_app_dependency("random", random)
        return runtime_random.choice(self.active_agents), user_input, False

    def _build_task_overview(self) -> dict:
        """Monta task overview."""
        return self.build_task_overview()

    def _task_context_history_window(self) -> int:
        """Executa task context history window."""
        return self.task_context_history_window()

    def _format_task_chat_context(self) -> str:
        """Formata task chat context."""
        return self.format_task_chat_context()

    def _build_task_body(self, description: str) -> str:
        """Monta task body."""
        return self.build_task_body(description)

    def _refresh_task_shared_state(self) -> None:
        """Atualiza task shared state."""
        self.refresh_task_shared_state()

    def _show_task_response(self, task_id: int, agent: str, response: str) -> None:
        """Exibe task response."""
        self.show_task_response(task_id, agent, response)

    @staticmethod
    def _parse_task_command(command: str) -> str:
        """Interpreta task command."""
        return QuimeraApp.parse_task_command(command)

    def _get_task_routing_plugins(self):
        """Retorna task routing plugins."""
        return self.get_task_routing_plugins()

    @staticmethod
    def _classify_task_execution_result(response: str | None) -> tuple[bool, str]:
        """Classifica task execution result."""
        return QuimeraApp.classify_task_execution_result(response)

    def _count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta agent open tasks."""
        return self.count_agent_open_tasks(agent_name)

    def _choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Seleciona agent with load balance."""
        return self.choose_agent_with_load_balance(task_type)

    def _handle_task_command(self, command: str) -> None:
        """Processa task command."""
        self.handle_task_command(command)

    @staticmethod
    def _merge_state_value(current, incoming):
        """Mescla state value."""
        return AppProtocol.merge_state_value(current, incoming)

    def _apply_state_update(self, block_content):
        """Executa apply state update."""
        return self._get_protocol().apply_state_update(self, block_content)

    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1

    def call_agent(self, agent, **options):
        """Executa call agent."""
        dispatch_options = dict(options)
        silent = dispatch_options.pop("silent", False)
        persist_history = dispatch_options.pop("persist_history", True)
        show_output = dispatch_options.pop("show_output", True)
        quiet = dispatch_options.pop("quiet", False)
        handoff = dispatch_options.get("handoff")
        handoff_id = handoff.get("handoff_id") if isinstance(handoff, dict) else None
        logger.info(
            "[DISPATCH] sending to agent=%s, handoff_only=%s, handoff_id=%s",
            agent, dispatch_options.get("handoff_only", False), handoff_id,
        )
        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            if self.agent_client:
                self.agent_client._user_cancelled = False
            try:
                response = self._call_agent(agent, silent=silent, **dispatch_options)
                if response is None:
                    if self.agent_client and self.agent_client._user_cancelled:
                        logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < self.MAX_RETRIES:
                        logger.warning("[DISPATCH] retry %d/%d for agent=%s", attempt, self.MAX_RETRIES, agent)
                        time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    self._record_failure(agent)
                    return None
                result = self.resolve_agent_response(
                    agent,
                    response,
                    silent=silent,
                    persist_history=persist_history,
                    show_output=show_output,
                )
                if result is None:
                    if self.agent_client and self.agent_client._user_cancelled:
                        logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                        return None
                    if attempt < self.MAX_RETRIES:
                        logger.warning("[DISPATCH] retry %d/%d for agent=%s (resolve failed)", attempt,
                                       self.MAX_RETRIES, agent)
                        time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    self._record_failure(agent)
                return result
            except Exception as exc:
                if self.agent_client and self.agent_client._user_cancelled:
                    logger.info("[DISPATCH] agent=%s cancelled by user, aborting", agent)
                    return None
                last_error = exc
                if attempt < self.MAX_RETRIES:
                    logger.warning("[DISPATCH] retry %d/%d for agent=%s after exception: %s", attempt, self.MAX_RETRIES,
                                   agent, exc)
                    time.sleep(self.RETRY_BACKOFF_SECONDS * attempt)
                    continue
                self._record_failure(agent)
                raise
        if last_error:
            logger.error("[DISPATCH] all retries exhausted for agent=%s", agent)
        return None

    def _call_agent(self, agent, is_first_speaker=False, handoff=None, primary=True, protocol_mode="standard",
                    handoff_only=False, silent=False, from_agent=None):
        """Executa call agent."""
        with self._counter_lock:
            self.session_call_index += 1
            call_index_snapshot = self.session_call_index
        start = time.time()
        history = [] if handoff_only else self.history
        self._get_task_services().refresh_task_shared_state()
        # Agentes com driver de API recebem tools via schema OpenAI — as instruções
        # text-based conflitariam com o protocolo da API e devem ser omitidas.
        plugin = plugins.get(agent)
        _driver = getattr(plugin, "driver", "cli") if plugin else "cli"
        skip_tool_prompt = isinstance(_driver, str) and _driver != "cli"
        if self.debug_prompt_metrics:
            prompt, metrics = self.prompt_builder.build(
                agent,
                history,
                is_first_speaker,
                handoff,
                debug=True,
                primary=primary,
                shared_state=self.shared_state,
                handoff_only=handoff_only,
                from_agent=from_agent,
                skip_tool_prompt=skip_tool_prompt,
            )
            self.agent_client.log_prompt_metrics(
                agent, metrics,
                session_id=self.session_state["session_id"],
                round_index=self.round_index,
                session_call_index=call_index_snapshot,
                history_window=self.prompt_builder.history_window,
                protocol_mode=protocol_mode,
            )
        else:
            prompt = self.prompt_builder.build(
                agent, history, is_first_speaker, handoff,
                primary=primary, shared_state=self.shared_state,
                handoff_only=handoff_only, from_agent=from_agent,
                skip_tool_prompt=skip_tool_prompt,
            )
        result = self.agent_client.call(agent, prompt, silent=silent)
        elapsed = time.time() - start
        if hasattr(self, 'session_state') and self.session_state:
            with self._counter_lock:
                try:
                    self.session_state["handoffs_sent"] += 1
                    self.session_state["total_latency"] += elapsed
                    if result:
                        self.session_state["handoffs_succeeded"] += 1
                    else:
                        self.session_state["handoffs_failed"] += 1
                except KeyError:
                    pass  # Old session state without metrics
            self._record_agent_metric(agent, "succeeded" if result else "failed", elapsed)
        logger.info("[DISPATCH] agent=%s latency=%.2fs result=%s", agent, elapsed, "ok" if result else "none")
        return result

    _PAYLOAD_FIELD_RE = re.compile(r"^\s*(task|context|expected)\s*:", re.IGNORECASE)

    @staticmethod
    def _strip_payload_residual(text):
        """Remove payload residual."""
        return AppProtocol(logger).strip_payload_residual(QuimeraApp, text)

    def _record_agent_metric(self, agent, metric_name, latency):
        """Registra agent metric."""
        self._get_session_metrics().record_agent_metric(self, agent, metric_name, latency)

    def _record_tool_event(self, agent, result=None, loop_abort=False, reason=None):
        """Registra métricas de uso de ferramentas atribuídas ao agente."""
        is_invalid = bool(getattr(result, "error", None)) and "Sem política para a ferramenta" in str(result.error)
        ok = bool(getattr(result, "ok", False))
        self._get_session_metrics().record_tool_event(
            self,
            agent,
            ok=ok,
            is_invalid=is_invalid,
            loop_abort=loop_abort,
        )

    def _has_clear_next_step(self, response):
        """Executa has clear next step."""
        return self._get_session_metrics().has_clear_next_step(response)

    def _is_response_redundant(self, response, history):
        """Executa is response redundant."""
        return self._get_session_metrics().is_response_redundant(response, history)

    @staticmethod
    def _generate_handoff_id(task, target, timestamp=None):
        """Executa generate handoff id."""
        return AppProtocol.generate_handoff_id(task, target, timestamp=timestamp)

    def parse_handoff_payload(self, payload, target=None):
        """Interpreta handoff payload."""
        return self._get_protocol().parse_handoff_payload(self, payload, target=target)

    def parse_response(self, response):
        """Interpreta response."""
        return self._get_protocol().parse_response(self, response)

    def _call_agent_for_parallel(self, agent, handoff, protocol_mode, staging_root, index):
        """Executa call_agent e retorna tupla (agent, response, route_target, handoff, extend, needs_input)."""
        set_staging_root(staging_root / str(index))
        try:
            raw = self.call_agent(agent, handoff=handoff, primary=False, protocol_mode=protocol_mode)
            response, route_target, handoff, extend, needs_input, _ = self.parse_response(raw)
            # Propaga o flag de necessidade de input humano (6º elemento)
            return agent, response, route_target, handoff, extend, needs_input
        finally:
            set_staging_root(None)

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

    def print_response(self, agent, response):
        """Executa print response."""
        with self._output_lock:
            self._clear_user_prompt_line_if_needed()
            if response is not None:
                self.renderer.show_message(agent, response)
            else:
                self.renderer.show_no_response(agent)
            self._redisplay_user_prompt_if_needed(clear_first=False)

    def persist_message(self, role, content):
        """Persiste uma mensagem no histórico em memória, log e snapshot JSON."""
        with self._lock:
            self.history.append({"role": role, "content": content})
            self.storage.append_log(role, content)
            self.storage.save_history(self.history, shared_state=self.shared_state)
            self._get_session_metrics().update_persisted_message_metrics(self, role, content)

    def _maybe_auto_summarize(self, preferred_agent=None):
        """Sumariza e trunca o histórico quando excede o threshold configurado."""
        threshold = getattr(self, "auto_summarize_threshold", None)
        if not isinstance(threshold, int) or threshold <= 0:
            return
        if len(self.history) < threshold:
            return

        keep = self.prompt_builder.history_window
        to_summarize = self.history[:-keep]
        recent = self.history[-keep:]
        existing_summary = self.context_manager.load_session_summary()

        self.renderer.show_system(
            f"[memória] histórico com {len(self.history)} mensagens — gerando resumo automático..."
        )
        summary_agent_preference = preferred_agent or getattr(
            self,
            "summary_agent_preference",
            self.active_agents[0],
        )
        summary = self.session_summarizer.summarize(
            to_summarize,
            existing_summary=existing_summary,
            preferred_agent=summary_agent_preference,
        )
        if summary:
            self.context_manager.update_with_summary(summary)
            self.history = recent
            self.storage.save_history(self.history, shared_state=self.shared_state)
            self.renderer.show_system(
                f"[memória] histórico truncado para {len(self.history)} mensagens recentes"
            )
        else:
            self.renderer.show_system("[memória] resumo automático falhou — histórico mantido")

    def shutdown(self):
        """Finaliza a sessão tentando resumir o histórico no contexto persistente."""
        self._stop_task_executors()
        runtime_readline = resolve_app_dependency("readline", readline)
        if runtime_readline:
            try:
                runtime_readline.write_history_file(str(self.history_file))
            except Exception:
                pass

        if not self.history:
            return

        self.renderer.show_system(MSG_MEMORY_SAVING)

        result = [None]

        def _run_summary():
            try:
                result[0] = self.session_summarizer.summarize(
                    self.history,
                    existing_summary=self.context_manager.load_session_summary(),
                    preferred_agent=getattr(self, "summary_agent_preference", None),
                )
            except Exception:
                pass

        t = threading.Thread(target=_run_summary, daemon=True)
        t.start()
        try:
            t.join(timeout=30)
        except KeyboardInterrupt:
            if self.agent_client:
                self.agent_client._user_cancelled = True
                self.agent_client._cancel_event.set()
            self.renderer.show_system(MSG_MEMORY_FAILED.strip())
            try:
                t.join(timeout=1)
            except KeyboardInterrupt:
                pass
            return
        summary = result[0]
        if summary:
            self.context_manager.update_with_summary(summary)
        else:
            self.renderer.show_system(MSG_MEMORY_FAILED)

    def _process_chat_message(self, user):
        """Executa process chat message com controle de turno."""
        try:
            # O turno já foi alternado para AI no run() ou _process_chat_queue
            self._do_process_chat_message(user)
        finally:
            if hasattr(self, "turn_manager") and self.turn_manager.is_ai_turn:
                self.turn_manager.next_turn()

    def _do_process_chat_message(self, user):
        """Implementação real do processamento de mensagens do chat."""
        first_agent, message, explicit = self.parse_routing(user)
        if first_agent is None:
            return
        if not message or not message.strip():
            self.renderer.show_warning(MSG_EMPTY_INPUT.format(first_agent))
            return

        # Se há um agente aguardando resposta humana e o usuário não especificou
        # explicitamente outro agente, redireciona para ele
        pending_input_for = getattr(self, "_pending_input_for", None)
        if pending_input_for and not explicit:
            first_agent = pending_input_for
        self._pending_input_for = None

        other_agents = [n for n in self.active_agents if n != first_agent]

        self.round_index += 1
        self.summary_agent_preference = first_agent
        self.persist_message(USER_ROLE, message)

        # Primeira fala: detecta roteamento ou debate estendido
        response = self.call_agent(first_agent, is_first_speaker=True, protocol_mode="standard")
        response, route_target, handoff, extend, needs_human_input, _ = self.parse_response(response)

        if self.agent_client and self.agent_client._user_cancelled:
            self.renderer.show_system("[cancelado] fluxo interrompido.")
            if hasattr(self, "turn_manager"):
                self.turn_manager.reset()
            return

        if response is None and not route_target and not needs_human_input:
            fallback_candidates = [agent for agent in self.active_agents if agent != first_agent]
            failed_agent = first_agent
            for fallback_agent in fallback_candidates:
                logger.info(
                    "[CHAT_FAILOVER] trying %s after %s returned no response",
                    fallback_agent,
                    failed_agent,
                )
                self.renderer.show_system(
                    f"[fallback] {failed_agent} não respondeu; {fallback_agent} assumiu"
                )
                fallback_response = self.call_agent(
                    fallback_agent,
                    is_first_speaker=True,
                    primary=False,
                    protocol_mode="standard",
                )
                fallback_response, route_target, handoff, extend, needs_human_input, _ = self.parse_response(
                    fallback_response
                )
                if fallback_response is None and not route_target and not needs_human_input:
                    continue
                first_agent = fallback_agent
                self.summary_agent_preference = first_agent
                response = fallback_response
                break

        if needs_human_input:
            if response:
                self.renderer.show_message(first_agent, response)
            self._pending_input_for = first_agent
            self.renderer.show_system(f"\nResponda para {first_agent.upper()}:\n")
            return
        self.print_response(first_agent, response)
        if response is not None:
            self.persist_message(first_agent, response)

        # Um handoff emitido pela primeira resposta sempre tem prioridade,
        # inclusive quando a rodada começou com /claude ou /codex.
        if route_target and handoff:
            handoff_id = handoff.get("handoff_id", "?")
            priority = handoff.get("priority", "normal")

            # Verifica delegação circular
            chain = handoff.get("chain", []) if isinstance(handoff, dict) else []
            if route_target in chain:
                logger.warning(
                    "[HANDOFF] Circular delegation detected: %s -> %s (chain: %s)",
                    first_agent, route_target, chain,
                )
                if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                    self.behavior_metrics.record_handoff_received(route_target, is_circular=True)
                self.renderer.show_warning(
                    f"Delegação circular detectada: {first_agent} -> {route_target}. "
                    f"Cadeia: {' -> '.join(chain + [route_target])}"
                )
                return

            self.renderer.show_handoff(
                first_agent,
                route_target,
                task=handoff["task"],
            )
            logger.info(
                "[HANDOFF] id=%s from=%s to=%s priority=%s chain=%s",
                handoff_id, first_agent, route_target, priority, chain,
            )
            # Propaga a cadeia de handoffs
            if isinstance(handoff, dict):
                handoff["chain"] = chain + [first_agent]
            # Registra métricas de handoff
            if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                self.behavior_metrics.record_handoff_sent(first_agent)
                self.behavior_metrics.record_handoff_received(route_target)
            # Handoff v1: agente secundário recebe apenas o payload delegado
            secondary_response = self.call_agent(
                route_target,
                handoff=handoff,
                handoff_only=True,
                primary=False,
                protocol_mode="handoff",
                from_agent=first_agent,
            )
            if self.agent_client and self.agent_client._user_cancelled:
                self.renderer.show_system("[cancelado] fluxo interrompido.")
                if hasattr(self, "turn_manager"):
                    self.turn_manager.reset()
                return
            expected_ack = handoff.get("handoff_id")
            secondary_response, _, _, _, _, ack_id = self.parse_response(secondary_response)
            if expected_ack and ack_id and ack_id != expected_ack:
                logger.warning(
                    "[ACK] mismatch: expected=%s, received=%s from agent=%s",
                    expected_ack, ack_id, route_target,
                )
            self.print_response(route_target, secondary_response)
            if secondary_response is not None:
                self.persist_message(route_target, secondary_response)

            # Fallback chain: se o agente secundário não respondeu, tenta próximo disponível
            if not secondary_response:
                fallback_candidates = [
                    a for a in self.active_agents
                    if a != first_agent and a != route_target and a not in chain
                ]
                for fallback_agent in fallback_candidates:
                    logger.info(
                        "[HANDOFF] id=%s fallback: trying %s after %s failed",
                        handoff_id, fallback_agent, route_target,
                    )
                    self.renderer.show_system(
                        f"[handoff] tentando fallback: {fallback_agent} (após {route_target} falhar)"
                    )
                    fallback_handoff = dict(handoff) if isinstance(handoff, dict) else handoff
                    if isinstance(fallback_handoff, dict):
                        fallback_handoff["chain"] = handoff.get("chain", []) + [route_target]
                    secondary_response = self.call_agent(
                        fallback_agent,
                        handoff=fallback_handoff,
                        handoff_only=True,
                        primary=False,
                        protocol_mode="handoff",
                        from_agent=first_agent,
                    )
                    if self.agent_client and self.agent_client._user_cancelled:
                        self.renderer.show_system("[cancelado] fluxo interrompido.")
                        if hasattr(self, "turn_manager"):
                            self.turn_manager.reset()
                        return
                    secondary_response, _, _, _, _, ack_id = self.parse_response(secondary_response)
                    if secondary_response:
                        route_target = fallback_agent
                        self.print_response(fallback_agent, secondary_response)
                        self.persist_message(fallback_agent, secondary_response)
                        break

            # Integrador: agente primário sintetiza com a resposta do secundário
            if secondary_response:
                synthesis_handoff = HANDOFF_SYNTHESIS_MSG.format(
                    agent=route_target.upper(),
                    task=handoff["task"],
                    response=secondary_response,
                )
                # Registra síntese no BehaviorMetricsTracker
                if hasattr(self, 'behavior_metrics') and self.behavior_metrics:
                    self.behavior_metrics.record_synthesis(first_agent)
                final_response = self.call_agent(
                    first_agent,
                    handoff=synthesis_handoff,
                    primary=False,
                    protocol_mode="handoff",
                )
                if self.agent_client and self.agent_client._user_cancelled:
                    self.renderer.show_system("[cancelado] fluxo interrompido.")
                    if hasattr(self, "turn_manager"):
                        self.turn_manager.reset()
                    return
                final_response, _, _, _, _, _ = self.parse_response(final_response)
                self.print_response(first_agent, final_response)
                if final_response is not None:
                    self.persist_message(first_agent, final_response)
            else:
                logger.warning(
                    "[HANDOFF] id=%s failed: secondary agent %s returned no response",
                    handoff_id, route_target,
                )
                self.renderer.show_system(
                    f"[handoff] {route_target} não respondeu — delegação falhou"
                )
        else:
            # Fluxo padrão: 2 falas. Estendido (EXTEND_MARKER): 4 falas alternadas.
            # Em rodadas com /claude ou /codex, o handoff do primeiro agente
            # já foi tratado no bloco acima. Aqui só decidimos se existe
            # continuação automática do fluxo normal.
            protocol_mode = "extended" if extend else "standard"
            if explicit or not extend:
                remaining = []
            else:
                remaining = [other_agents[0], first_agent, other_agents[0]] if other_agents else []

            next_handoff = None
            if self.threads > 1 and len(remaining) > 1:
                # Modo paralelo: executar agentes em paralelo
                staging_root = Path(
                    tempfile.gettempdir()) / "quimera-staging" / f"{self.session_state['session_id']}-round{self.round_index}"
                staging_root.mkdir(parents=True, exist_ok=True)
                logger.info("parallel mode: %d threads, staging=%s", self.threads, staging_root)
                native_tool_agents = [
                    a for a in remaining
                    if getattr(plugins.get(a), "output_format", None) == "stream-json"
                ]
                if native_tool_agents:
                    logger.warning(
                        "[parallel] agentes com tools nativas não usam staging: %s — "
                        "escritas de arquivo vão direto ao disco e podem conflitar",
                        native_tool_agents,
                    )
                try:
                    with ThreadPoolExecutor(max_workers=self.threads) as executor:
                        # Criar lista de (agent, handoff, staging_dir, index)
                        agent_handoff_pairs = [(agent, None, staging_root, i) for i, agent in enumerate(remaining)]
                        # Executar em paralelo
                        futures = [
                            executor.submit(
                                self._call_agent_for_parallel,
                                agent,
                                handoff,
                                protocol_mode,
                                staging_dir,
                                idx,
                            )
                            for agent, handoff, staging_dir, idx in agent_handoff_pairs
                        ]
                        results = [f.result() for f in futures]
                    # Merge ordenado para o workspace
                    self._merge_staging_to_workspace(staging_root)
                    if self.agent_client and self.agent_client._user_cancelled:
                        self.renderer.show_system("[cancelado] fluxo interrompido.")
                        if hasattr(self, "turn_manager"):
                            self.turn_manager.reset()
                        return
                    # Processar resultados na ordem original
                    needs_input_any = False
                    # Expecting 6-tuple now: (agent, response, route_target, handoff, extend, needs_input)
                    for item in results:
                        agent, response, route_target, handoff, extend, needs_input = item
                        self.print_response(agent, response)
                        if response is not None:
                            self.persist_message(agent, response)
                        needs_input_any = needs_input or needs_input_any
                    # Nota: route_target e handoff podem ser ignorados no modo paralelo
                    if needs_input_any:
                        needing = next((a for a in results if a[-1]), None)
                        if needing:
                            current_agent = needing[0]
                            self._pending_input_for = current_agent
                            self.renderer.show_system(f"\nResponda para {current_agent.upper()}:\n")
                except Exception as exc:
                    logger.exception("parallel stage failed: %s", exc)
                    raise
                finally:
                    if staging_root.exists():
                        shutil.rmtree(staging_root)
                        logger.info("staging cleanup: %s removed", staging_root)
            else:
                # Modo sequencial (original)
                for index, agent in enumerate(remaining):
                    response = self.call_agent(agent, handoff=next_handoff, primary=False, protocol_mode=protocol_mode)
                    if self.agent_client and self.agent_client._user_cancelled:
                        self.renderer.show_system("[cancelado] fluxo interrompido.")
                        if hasattr(self, "turn_manager"):
                            self.turn_manager.reset()
                        return
                    next_handoff = None
                    response, route_target, handoff, _, needs_human_input, _ = self.parse_response(response)
                    self.print_response(agent, response)
                    if response is not None:
                        self.persist_message(agent, response)
                    if needs_human_input:
                        self._pending_input_for = agent
                        self.renderer.show_system(f"\nResponda para {agent.upper()}:\n")
                        break
                    if route_target and index + 1 < len(remaining):
                        remaining[index + 1] = route_target
                    if route_target:
                        next_handoff = handoff

        self._maybe_auto_summarize(preferred_agent=first_agent)

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
        if self.agent_client:
            self.agent_client._user_cancelled = False
        self.renderer.show_system(MSG_CHAT_STARTED)
        self.renderer.show_system(
            MSG_SESSION_STATUS.format(
                session_id=self.session_state["session_id"],
                history_count=self.session_state["history_count"],
                summary_loaded=self._format_yes_no(self.session_state["summary_loaded"]),
            )
        )
        self.renderer.show_system(MSG_SESSION_LOG.format(self.storage.get_log_file()))

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

                user = self.read_user_input(f"{self.user_name}: ", timeout=0)
                if user is None:
                    if not sys.stdin.isatty():
                        break
                    continue

                if user == CMD_EXIT:
                    break

                if user.strip() == CMD_EDIT:
                    content = self.read_from_editor()
                    if not content:
                        continue
                    user = content

                elif user.strip().startswith(CMD_FILE_PREFIX):
                    path_str = user.strip()[len(CMD_FILE_PREFIX):]
                    content = self.read_from_file(path_str)
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
            self.renderer.show_system(MSG_SHUTDOWN)
        finally:
            try:
                if threaded_chat and chat_queue is not None:
                    chat_queue.put(None)
                if chat_worker is not None:
                    chat_worker.join(timeout=0.5)
            except KeyboardInterrupt:
                pass
            self.shutdown()
