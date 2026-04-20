"""Componentes de `quimera.app.core`."""
import os
import queue
import random
import shutil
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None

from .handlers import PromptAwareStderrHandler
from .chat_round import ChatRoundOrchestrator
from .protocol import AppProtocol
from .session import AppSessionServices
from .session_metrics import SessionMetricsService
from .dispatch import AppDispatchServices
from .inputs import AppInputServices
from .task import AppTaskServices, call_agent_for_parallel, create_executor
from .system_layer import AppSystemLayer
from .turn import TurnManager
from .. import plugins
from ..runtime.parser import strip_tool_block
from ..runtime import tasks as runtime_tasks
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
    CMD_AGENTS, CMD_ALIASES, CMD_CLEAR, CMD_CONTEXT, CMD_CONTEXT_EDIT, CMD_EDIT, CMD_EXIT,
    CMD_FILE_PREFIX, CMD_HELP,
    CMD_PROMPT, CMD_TASK,
    MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_SHUTDOWN, MSG_DOUBLE_PREFIX,
)
from ..modes import MODES, get_mode
from .config import logger


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""

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
                 workspace: Workspace | None = None,
                 ):
        """Inicializa uma instância de QuimeraApp."""
        self.selected_agents = list(agents) if agents else []
        self.active_agents = self.selected_agents
        self.threads = int(threads) if threads is not None else 1
        self.agent_failures = defaultdict(int)
        self._agent_failures_lock = threading.Lock()
        self.workspace = workspace if workspace is not None else Workspace(cwd)
        self.config = ConfigManager(self.workspace.config_file)
        _active_theme = theme if theme is not None else self.config.theme
        self.renderer = TerminalRenderer(theme=_active_theme)
        self.user_name = self.config.user_name
        self.spy = spy
        self.system_layer = AppSystemLayer(self)
        self.protocol = AppProtocol(self, decisions_log_path=self.workspace.decisions_log)
        self.session_metrics = SessionMetricsService()
        self.task_services = AppTaskServices(self)
        self.dispatch_services = AppDispatchServices(self)
        self.session_services = AppSessionServices(self)
        self.readline = readline
        self.input_services = AppInputServices(
            self,
            input_resolver=lambda: input,
        )
        self.chat_round_orchestrator = ChatRoundOrchestrator(self)

        # Configuração do histórico persistente (readline)
        self.history_file = self.workspace.history_file
        runtime_readline = readline
        if runtime_readline:
            if self.history_file.exists():
                try:
                    runtime_readline.read_history_file(str(self.history_file))
                except Exception:
                    pass
            runtime_readline.set_history_length(1000)
            self._configure_readline_completion(runtime_readline)

        migrated = self.workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = ContextManager(
            self.workspace.context_persistent,
            self.workspace.context_session,
            self.renderer,
        )
        self.storage = SessionStorage(self.workspace.logs_dir, self.renderer)
        session_id = self.storage.get_history_file().stem
        metrics_file = self.workspace.metrics_dir / f"{session_id}.jsonl" if debug else None
        self.agent_client = AgentClient(
            self.renderer,
            metrics_file=metrics_file,
            timeout=timeout,
            spy=self.spy,
            working_dir=str(self.workspace.cwd),
        )
        self.task_executor_factory = create_executor
        self.session_summarizer = SessionSummarizer(
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
        self.behavior_metrics = BehaviorMetricsTracker(storage_path=metrics_state_path)
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
        self.prompt_builder = PromptBuilder(
            self.context_manager,
            history_window=history_window or self.config.history_window,
            session_state=session_state,
            user_name=self.user_name,
            active_agents=self.active_agents,
            metrics_tracker=self.behavior_metrics,
        )
        self.auto_summarize_threshold = self.config.auto_summarize_threshold
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds

        self.tool_executor = self.task_services.build_tool_executor()
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
            CMD_CLEAR,
            CMD_CONTEXT,
            CMD_CONTEXT_EDIT,
            CMD_EDIT,
            CMD_EXIT,
            CMD_FILE_PREFIX,
            CMD_HELP,
            CMD_PROMPT,
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

    def get_agent_plugin(self, agent_name: str):
        """Resolve um plugin pelo nome canônico do agente."""
        if not agent_name:
            return None
        return plugins.get(agent_name)

    def get_available_plugins(self) -> list:
        """Retorna a lista atual de plugins conhecidos pela aplicação."""
        return list(plugins.all_plugins())

    def get_active_agent_plugins(self) -> list:
        """Retorna os plugins válidos dos agentes ativos na sessão."""
        active_plugins = []
        for agent_name in self.active_agents:
            plugin = self.get_agent_plugin(agent_name)
            if plugin is not None:
                active_plugins.append(plugin)
        return active_plugins

    def _configure_readline_completion(self, runtime_readline) -> None:
        """Registra autocomplete de comandos slash quando readline estiver disponível."""
        if runtime_readline is None:
            return

        def completer(text: str, state: int) -> str | None:
            if not text.startswith("/"):
                return None
            matches = [cmd for cmd in self._available_commands() if cmd.startswith(text)]
            return matches[state] if state < len(matches) else None

        try:
            runtime_readline.set_completer_delims(" \t\n")
            runtime_readline.set_completer(completer)
            runtime_readline.parse_and_bind("tab: complete")
        except Exception:
            logger.debug("falha ao configurar autocomplete do readline", exc_info=True)

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
        session_metrics = getattr(self, "session_metrics", None)
        if session_metrics is not None:
            session_metrics.record_agent_metric(self, agent, "failed", 0)

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
        self.task_services.setup_task_executors()

    def _stop_task_executors(self):
        """Executa stop task executors."""
        self.task_services.stop_task_executors()

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
            runtime_readline = readline
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
        return AppTaskServices.parse_task_command(command)

    @staticmethod
    def classify_task_execution_result(response: str | None) -> tuple[bool, str]:
        """Return whether the task execution can be considered completed."""
        return AppTaskServices.classify_task_execution_result(response)

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
                self.active_agents = self.selected_agents
            return random.choice(self.active_agents), "", False

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
            self.active_agents = self.selected_agents
        return random.choice(self.active_agents), user_input, False

    @staticmethod
    def _merge_state_value(current, incoming):
        """Mescla state value."""
        return AppProtocol.merge_state_value(current, incoming)

    def _apply_state_update(self, block_content):
        """Executa apply state update."""
        return self.protocol.apply_state_update(block_content)

    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1

    @staticmethod
    def _strip_payload_residual(text):
        """Remove payload residual."""
        return AppProtocol().strip_payload_residual(text)

    def _record_tool_event(self, agent, result=None, loop_abort=False, reason=None):
        """Registra métricas de uso de ferramentas atribuídas ao agente."""
        is_invalid = bool(getattr(result, "error", None)) and "Sem política para a ferramenta" in str(result.error)
        ok = bool(getattr(result, "ok", False))
        self.session_metrics.record_tool_event(self, agent, ok=ok, is_invalid=is_invalid, loop_abort=loop_abort)

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

    def read_user_input(self, prompt, timeout: int):
        """Fachada compatível para leitura de input."""
        return self.input_services.read_user_input(prompt, timeout)

    def handle_command(self, user_input: str) -> bool:
        """Fachada compatível para comandos slash."""
        return self.system_layer.handle_command(user_input)

    def show_system_message(self, message: str) -> None:
        """Fachada compatível para mensagens de sistema."""
        self.system_layer.show_system_message(message)

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

    def _call_agent_for_parallel(self, agent, handoff, protocol_mode, staging_root, index):
        """Executa call_agent e retorna tupla (agent, response, route_target, handoff, extend, needs_input)."""
        return call_agent_for_parallel(self, agent, handoff, protocol_mode, staging_root, index)

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
            self.renderer.show_system(MSG_SHUTDOWN)
        finally:
            try:
                if threaded_chat and chat_queue is not None:
                    chat_queue.put(None)
                if chat_worker is not None:
                    chat_worker.join(timeout=0.5)
            except KeyboardInterrupt:
                pass
            self.session_services.shutdown()
