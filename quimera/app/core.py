"""Componentes de `quimera.app.core`."""
import json
import os
import platform
import sys
import threading
import time
from contextlib import nullcontext

from pathlib import Path

from .agent_pool import AgentPool, AgentPoolView
from .handlers import PromptAwareStderrHandler
from ..domain.session_state import SessionState
from .chat_round import ChatRoundOrchestrator
from .chat_lifecycle import ChatLifecycle
from .chat_processor import run_chat_loop
from .protocol import AppProtocol

from .session import AppSessionServices, compute_history_hard_limit, trim_history_messages
from .staging import merge_staging_to_workspace
from .session_bootstrap import (
    resolve_session_log_path,
    resolve_render_debug_log_path,
    resolve_workspace_render_log_path,
    resolve_workspace_render_ansi_path,
    resolve_workspace_metrics_path,
    resolve_app_log_path,
)
from .config import set_app_log_file
from .toolbar import ToolbarManager
from .toolbar_coordinator import ToolbarCoordinator
from .session_metrics import SessionMetricsService
from .dispatch import AppDispatchServices
from .inputs import AppInputServices
from .interfaces import PluginResolverAdapter
from .prompt_input import InputGate, PromptFormatter
from ..runtime.input_broker import InputBroker
from .runtime_state import AppRuntimeState
from ..tasks.classifiers import (
    classify_task_execution_result,
    classify_task_review_result,
    parse_task_command,
)
from ..tasks.events import (
    TaskStarted,
    TaskCompleted,
    TaskFailed,
    TaskProposed,
    TaskSubmittedForReview,
    TaskRequeued,
)
from ..tasks.services import AppTaskServices
from ..tasks.utils import summarize_task_feedback
from ..tasks.executor import create_executor
from .display_service import DisplayService
from .system_layer import AppSystemLayer
from .turn import TurnManager
from .event_sink import EventSink
from .ui_event_handler import UiEventHandler
from .worker import ChatWorker
from .. import plugins
from ..plugins.base import PluginRegistry
from ..tasks import api as runtime_tasks
from ..runtime.process_supervisor import ProcessSupervisor
from ..ui import RenderAuditLogger, TerminalRenderer
from ..context import ContextManager
from ..storage import SessionStorage
from ..agents import AgentClient
from ..session_summary import SessionSummarizer, build_chain_summarizer
from ..prompt import PromptBuilder
from ..workspace import Workspace
from ..config import ConfigManager, DEFAULT_USER_NAME
from ..env_config import EnvConfig
from ..metrics import BehaviorMetricsTracker
from ..bugs import (
    AgentRuntimeBugDetector,
    BugCorrelator,
    BugEvidenceRef,
    BugReport,
    BugStore,
    RenderBugDetector,
)
from ..constants import (
    CMD_AGENTS, CMD_ALIASES, CMD_BUGS, CMD_CLEAR, CMD_CONNECT, CMD_DISCONNECT, CMD_CONTEXT, CMD_EDIT, CMD_EXIT,
    CMD_APPROVE, CMD_APPROVE_ALL, CMD_FILE_PREFIX, CMD_HELP,
    CMD_PROMPT, CMD_RELOAD, CMD_RESET, CMD_TASK,
    MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_SHUTDOWN,
    Visibility,
)
from ..modes import MODES
from ..shared_state import bootstrap_state_key_stamps, clear_agent_state_for_session_start
from .session_state import SessionStateManager
from .agent_failure_tracker import AgentFailureTracker
from .bug_services import BugServices
from .command_router import CommandRouter
from .config import logger


def normalize_agent_name(agent):
    """Normaliza identificador de agente para nome canônico string."""
    if hasattr(agent, "name"):
        return getattr(agent, "name")
    return agent


class QuimeraApp:
    """Orquestra comandos locais, roteamento entre agentes e ciclo da sessão."""
    _SESSION_LOG_DISPLAY_MAX_CHARS = 96

    def __init__(self,
                 cwd: Path,
                 debug: bool = False,
                 history_window: int | None = None,
                 agents: list | None = None,
                 threads: int = 1,
                 idle_timeout_seconds: int | None = None,
                 visibility: Visibility = Visibility.SUMMARY,
                 theme: str | None = None,
                 workspace: Workspace | None = None,
                 auto_approve_mutations: bool = False,
                 plugin_registry: PluginRegistry | None = None,
                 ):
        """Inicializa uma instância de QuimeraApp."""
        self._lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self.selected_agents = list(agents) if agents else []
        self.agent_pool = AgentPool(self.selected_agents)
        self.threads = int(threads) if threads is not None else 1
        self.toolbar = ToolbarManager(threads=self.threads)
        self.auto_approve_mutations = auto_approve_mutations
        self._plugin_registry = plugin_registry
        self.workspace = workspace if workspace is not None else Workspace(cwd)
        EnvConfig(self.workspace.env_file).apply_to_environ()
        self.config = ConfigManager(self.workspace.config_file)
        active_theme = theme if theme is not None else self.config.theme
        self.storage = SessionStorage(self.workspace.logs_dir)
        self._session_started_at = time.monotonic()
        self.bug_store = BugStore(self.workspace.tmp.logs_dir)
        self.bug_detector = RenderBugDetector(repeat_threshold=2)
        self.agent_bug_detector = AgentRuntimeBugDetector()
        self.bug_correlator = BugCorrelator(window_seconds=60.0)
        session_id = self.storage.session_id
        render_log_path = resolve_workspace_render_log_path(self.workspace, session_id)
        render_ansi_path = resolve_workspace_render_ansi_path(self.workspace, session_id)
        metrics_file = resolve_workspace_metrics_path(self.workspace, session_id) if debug else None
        app_log_path = resolve_app_log_path(self.workspace, session_id)
        if app_log_path:
            set_app_log_file(app_log_path)
        render_audit_logger = (
            RenderAuditLogger(render_log_path, render_ansi_path) if debug else None
        )
        self.renderer = TerminalRenderer(
            theme=active_theme,
            get_plugin_style=self._resolve_plugin_style,
            density=self.config.density,
            audit_logger=render_audit_logger,
        )
        self.event_sink = EventSink()
        self.user_name = self.config.user_name
        self.visibility = Visibility(visibility)
        self.session_metrics = SessionMetricsService()
        self.task_services = None
        self.task_executors = []
        self._approval_handler = None
        self.session_services = None
        self.execution_mode = None
        self.task_classifier = None
        self.tool_executor = None
        self.dispatch_services = None
        self.history_file = self.workspace.history_file
        self.input_gate = InputGate(
            renderer=self.renderer,
            history_file=self.history_file,
            command_resolver=self._available_commands,
            argument_resolver=self._command_argument_resolver,
        )
        self.input_broker = InputBroker(
            renderer=self.renderer,
            input_gate=self.input_gate,
        )
        self.context_manager = ContextManager(
            self.workspace.context_persistent,
            self.workspace.context_session,
            self.renderer,
            workspace=self.workspace,
        )
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
        self.session_state_mgr = SessionStateManager(
            storage=self.storage,
            shared_state=last_session["shared_state"],
            history=self.history,
        )
        self.shared_state = self.session_state_mgr.shared_state
        self._turn_stamps = self.session_state_mgr.turn_stamps
        self._shared_state_lock = self.session_state_mgr.shared_state_lock
        self._history_lock = self.session_state_mgr.history_lock
        history_restored = bool(self.history)
        clear_agent_state_for_session_start(self.shared_state, history_restored=history_restored)
        bootstrap_state_key_stamps(
            self.shared_state,
            self._turn_stamps,
            current_turn=int(self.shared_state.get("_current_turn", 0) or 0),
        )
        self._display_service = DisplayService(
            renderer=self.renderer,
            input_status_getter=self.input_gate.is_active,
            redisplay_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=self._output_lock,
            prompt_owner_thread_id_getter=self.input_gate.get_owner_thread_id,
            run_above_active_prompt=self.input_gate.run_in_terminal_message,
        )
        self._plugin_resolver = PluginResolverAdapter(
            registry=self._plugin_registry,
            normalize=normalize_agent_name,
        )
        self.system_layer = AppSystemLayer(
            display_service=self._display_service,
            plugin_resolver=self._plugin_resolver,
            prompt_builder=None,
            history_getter=self.session_state_mgr.history_snapshot,
            shared_state_getter=self.session_state_mgr.shared_state_snapshot,
            execution_mode_getter=lambda: getattr(self, "execution_mode", None),
            agent_pool=self.agent_pool,
            get_selected_agents=lambda: list(getattr(self, "selected_agents", []) or []),
            set_selected_agents=lambda agents: setattr(self, "selected_agents", list(agents)),
            clear_screen=self.clear_terminal_screen,
            read_user_input=self.read_user_input,
            task_command_handler=None,
            bugs_command_handler=self._handle_bugs_command,
            session_state_manager=self.session_state_mgr,
            approval_handler_getter=lambda: getattr(self, "_approval_handler", None),
            context_manager=self.context_manager,
            plugin_registry=self._plugin_registry,
        )
        self.input_services = AppInputServices(
            self.renderer,
            input_resolver=lambda: self.input_gate,
            get_input_status=self.input_gate.is_active,
            set_input_status=lambda v: setattr(self.runtime_state, 'nonblocking_input_status', v),
            set_prompt_text=lambda v: setattr(self.runtime_state, 'nonblocking_prompt_text', v),
            set_prompt_owner=lambda v: setattr(self.runtime_state, 'prompt_owning_thread_id', v),
            set_prompt_visible=lambda v: setattr(self.runtime_state, 'nonblocking_prompt_visible', v),
            flush_deferred_messages=self.system_layer.flush_deferred_messages,
            output_lock=self._output_lock,
        )
        self.renderer.set_prompt_integration(
            is_active_fn=self.input_gate.is_active,
            run_above_fn=self.input_gate.run_in_terminal_message,
        )
        migrated = self.workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        workspace_tmp = getattr(self.workspace, "tmp", None)
        workspace_tmp_root = getattr(workspace_tmp, "root", None)
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds
        self.process_supervisor = ProcessSupervisor()
        self.agent_client = AgentClient(
            self.renderer,
            metrics_file=metrics_file,
            idle_timeout=self.idle_timeout_seconds,
            visibility=self.visibility,
            working_dir=str(self.workspace.cwd),
            error_reporter=self.system_layer.show_error_message,
            muted_reporter=self.system_layer.show_muted_message,
            session_id=session_id,
            workspace_tmp_root=workspace_tmp_root,
            process_supervisor=self.process_supervisor,
            pause_idle_if=self._has_mcp_pending,
        )
        self.task_executor_factory = create_executor
        self.session_summarizer = SessionSummarizer(
            self.renderer,
            summarizer_call=build_chain_summarizer(
                self.agent_client,
                lambda: list(dict.fromkeys(self.agent_pool.agents)),
            ),
        )
        session_context = self.context_manager.load_session()
        summary_loaded = self.context_manager.SUMMARY_MARKER in session_context
        self.session_state = {
            "session_id": session_id,
            "history_count": len(self.history),
            "history_restored": history_restored,
            "summary_loaded": summary_loaded,
            "delegations_sent": 0,
            "delegations_received": 0,
            "delegations_succeeded": 0,
            "delegations_failed": 0,
            "total_latency": 0.0,
            "agent_metrics": {},
            "rounds_without_progress": 0,
            "consecutive_redundant_responses": 0,
            "delegation_invalid_count": 0,
            "responses_with_clear_next_step": 0,
            "total_responses": 0,
        }
        # Persist metrics state to workspace so agents can resume with previous metrics
        metrics_state_path = self.workspace.state_dir / "metrics_state.json"
        self.behavior_metrics = BehaviorMetricsTracker(storage_path=metrics_state_path)
        self.agent_client.tool_event_callback = self._record_tool_event
        self.debug_prompt_metrics = debug
        self._chat_state = SessionState(
            history=self.history,
            shared_state=self.shared_state,
            session_meta=self.session_state,
            shared_state_lock=self._shared_state_lock,
        )
        self._chat_state.summary_agent_preference = self.agent_pool.primary
        self.protocol = AppProtocol(
            lock=self._shared_state_lock,
            shared_state=self.shared_state,
            workspace=self.workspace,
            decisions_log_path=self.workspace.decisions_log,
            turn_stamps=self._turn_stamps,
        )
        self.runtime_state = AppRuntimeState()
        self._deferred_system_messages: list[str] = []
        self._MAX_DEFERRED_SYSTEM_MESSAGES = 20
        self.turn_manager = TurnManager()
        for handler in logger.handlers:
            if isinstance(handler, PromptAwareStderrHandler):
                handler.bind_callbacks(
                    output_lock=self._output_lock,
                    redisplay_prompt=self._redisplay_user_prompt_if_needed,
                    show_error=self.system_layer.show_error_message,
                    show_warning=self.system_layer.show_warning_message,
                    show_system=self.system_layer.show_system_message,
                    show_muted=self.system_layer.show_muted_message,
                    is_reading=self.input_gate.is_active,
                    debug_enabled=lambda: bool(self.debug_prompt_metrics),
                )
        is_new_session = not history_restored and not summary_loaded

        # Unify tasks database path
        self.tasks_db_path = str(self.workspace.tasks_db)
        runtime_tasks.init_db(self.tasks_db_path)
        self.current_job_id = runtime_tasks.add_job(f"Session {session_id}", db_path=self.tasks_db_path)
        self.session_state["current_job_id"] = self.current_job_id
        self._previous_current_job_id_env = os.environ.get("QUIMERA_CURRENT_JOB_ID")
        os.environ["QUIMERA_CURRENT_JOB_ID"] = str(self.current_job_id)

        session_state = {
            "session_id": self.session_state["session_id"],
            "is_new_session": self._format_yes_no(is_new_session),
            "history_restored": self._format_yes_no(history_restored),
            "summary_loaded": self._format_yes_no(summary_loaded),
            "current_job_id": self.current_job_id,
            "workspace_root": str(self.workspace.cwd),
            "workspace_data_root": str(self.workspace.root / "data"),
            "workspace_tmp_root": str(workspace_tmp_root) if workspace_tmp_root is not None else "",
            "current_dir": ".",
            "os_info": f"{platform.system()} {platform.release()}",
            "render_debug_active": debug,
            "render_log_path": str(render_log_path) if debug else "",
            "render_ansi_path": str(render_ansi_path) if debug else "",
            "metrics_path": str(metrics_file) if metrics_file else "",
            "app_log_path": str(app_log_path) if app_log_path else "",
            "mcp_enabled": False,
            "mcp_socket_path": "",
        }
        self.prompt_builder = PromptBuilder(
            self.context_manager,
            history_window=configured_history_window,
            session_state=session_state,
            user_name=self.user_name,
            active_agents=self.agent_pool.agents,
            active_agents_provider=lambda: self.agent_pool.agents,
            metrics_tracker=self.behavior_metrics,
        )
        self.system_layer._prompt_builder = self.prompt_builder
        self.auto_summarize_threshold = configured_auto_summarize_threshold
        self.task_services = AppTaskServices(
            task_executor_factory=self.task_executor_factory,
            current_job_id=self.current_job_id,
            agent_pool=self.agent_pool,
            task_executors=self.task_executors,
            renderer=self.renderer,
            input_services=self.input_services,
            input_gate=self.input_gate,
            event_sink=self.event_sink,
            agent_client=self.agent_client,
            workspace=self.workspace,
            get_dispatch_tool_executor=lambda: self.tool_executor,
            get_dispatch_services=lambda: self.dispatch_services,
            auto_approve_mutations=self.auto_approve_mutations,
            approval_handler=self._approval_handler,
            get_agent_plugin=self._plugin_resolver.get,
            available_plugins=self._plugin_resolver.plugins,
            session_state=self._chat_state,
            system_layer=self.system_layer,
            task_classifier=self.task_classifier,
            user_name=self.user_name,
            prompt_builder=self.prompt_builder,
            visibility=self.visibility,
            show_error_message=self.system_layer.show_error_message,
            show_muted_message=self.system_layer.show_muted_message,
            get_execution_mode=lambda: self.execution_mode,
            record_tool_event=self._record_tool_event,
            record_failure=self.record_failure,
            session_metrics=self.session_metrics,
            get_debug_prompt_metrics=lambda: self.debug_prompt_metrics,
            redisplay_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=self._output_lock,
            counter_lock=self._counter_lock,
            get_session_services=lambda: self.session_services,
            max_retries=self.MAX_RETRIES,
            retry_backoff_seconds=self.RETRY_BACKOFF_SECONDS,
            rate_limit_backoff_seconds=getattr(self, 'RATE_LIMIT_BACKOFF_SECONDS', 30),
            parse_response=self.parse_response,
            classify_task_execution_result=self.classify_task_execution_result,
            classify_task_review_result=classify_task_review_result,
        )
        self.system_layer.task_command_handler = self.task_services.handle_task_command
        self.session_services = AppSessionServices(
            session_state=self._chat_state,
            storage=self.storage,
            renderer=self.renderer,
            agent_pool=self.agent_pool,
            context_manager=self.context_manager,
            session_summarizer=self.session_summarizer,
            task_services=self.task_services,
            prompt_builder=self.prompt_builder,
            auto_summarize_threshold=self.auto_summarize_threshold,
            summary_agent_preference=self.summary_agent_preference,
            agent_client=self.agent_client,
        )
        self.dispatch_services = AppDispatchServices(
            prompt_builder=self.prompt_builder,
            renderer=self.renderer,
            get_agent_plugin=self._plugin_resolver.get,
            session_state=self._chat_state,
            get_execution_mode=lambda: self.execution_mode,
            refresh_task_state=self.task_services.refresh_task_shared_state,
            debug_prompt_metrics=self.debug_prompt_metrics,
            redisplay_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=self._output_lock,
            counter_lock=self._counter_lock,
            print_response_fn=self.print_response,
            persist_message_fn=lambda agent, text: self.session_services.persist_message(agent, text),
            record_session_metric=lambda agent, metric, elapsed: self.session_metrics.record_agent_metric(
                self, agent, metric, elapsed
            ),
            record_tool_event_fn=lambda agent, **kw: self.session_metrics.record_tool_event(self, agent, **kw),
            max_retries=self.MAX_RETRIES,
            retry_backoff=self.RETRY_BACKOFF_SECONDS,
            rate_limit_backoff=getattr(self, 'RATE_LIMIT_BACKOFF_SECONDS', 30),
            record_failure=self.record_failure,
            record_success=self.record_success,
            get_agent_client=lambda: self.agent_client,
            get_tool_executor=lambda: self.tool_executor,
        )
        self.task_services.bind_session_services(self.session_services)
        self.task_services.bind_dispatch_services(self.dispatch_services)
        self.chat_round_orchestrator = ChatRoundOrchestrator(
            dispatch_services=self.dispatch_services,
            parse_routing=self.parse_routing,
            agent_pool=self.agent_pool,
            session_services=self.session_services,
            parse_response=self.parse_response,
            agent_client=self.agent_client,
            turn_manager=self.turn_manager,
            task_services=self.task_services,
            get_agent_plugin=self._plugin_resolver.get,
            behavior_metrics=self.behavior_metrics,
            threads=self.threads,
            session_state=self._chat_state,
            show_system_message=self.system_layer.show_system_message,
            renderer=self.renderer,
            merge_staging_to_workspace=merge_staging_to_workspace,
        )
        self.tool_executor = self.task_services.build_tool_executor(require_approval_for_mutations=not self.auto_approve_mutations)
        self.task_services.bind_dispatch_tool_executor(self.tool_executor)
        self.task_services.bind_primary_approval_handler(self._approval_handler)
        # Conecta o InputBroker ao ConsoleApprovalHandler para serializar
        # approval e ask_user na mesma fila com timeout e auto-resposta segura.
        _pre_handler = getattr(self.tool_executor, "_approval_handler", None)
        _base_handler = getattr(_pre_handler, "_base", _pre_handler)
        _set_broker = getattr(_base_handler, "set_input_broker", None)
        if callable(_set_broker):
            _set_broker(self.input_broker)
        # Injeta o executor nos drivers de API do agent_client.
        self.agent_client.tool_executor = self.tool_executor
        self.tool_executor.set_delegate_fn(self.dispatch_services.delegate)
        # background_delegate_fn usa AgentClient isolado (cancel_event próprio),
        # impedindo que Ctrl+C no fluxo do chat cancele delegates assíncronos
        # e que o delegate assíncrono afete o fluxo principal.
        self.tool_executor.set_background_delegate_fn(
            lambda agent, **opts: (
                self.task_services._get_background_dispatch_services() or self.dispatch_services
            ).delegate(agent, **opts)
        )
        self.tool_executor.set_active_agents_provider(lambda: list(self.agent_pool.agents))
        self.tool_executor.set_cancel_checker(lambda: bool(getattr(self.agent_client, "_cancel_event", None) and self.agent_client._cancel_event.is_set()))
        self.tool_executor.set_agent_cleanup_callback(self._cleanup_sub_agent_stream)
        _broker = self.input_broker
        self.tool_executor.set_ask_user_fn(
            lambda q, opts: _broker.request_ask_user(q, opts)
        )
        # Set up task executors for autonomous task execution
        self._setup_task_executors()
        self._ui_event_handler = UiEventHandler(
            renderer=self.renderer,
            input_gate=self.input_gate,
            runtime_state=self.runtime_state,
            system_layer=self.system_layer,
            event_sink=self.event_sink,
            show_muted_message=self.system_layer.show_muted_message,
            show_system_message=self.system_layer.show_system_message,
            show_warning_message=self.system_layer.show_warning_message,
            show_error_message=self.system_layer.show_error_message,
            redisplay_user_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=self._output_lock,
        )
        self.toolbar_coordinator = ToolbarCoordinator(
            toolbar_manager=self.toolbar,
            agent_pool=self.agent_pool,
            get_agent_plugin=self._plugin_resolver.get,
            workspace=self.workspace,
            get_history=lambda: self.history,
            storage=self.storage,
            bug_store=self.bug_store,
            get_session_started_at=lambda: self._session_started_at,
            renderer=self.renderer,
            config=self.config,
            runtime_state=self.runtime_state,
            input_gate=self.input_gate,
            get_pending_input_for=lambda: self._chat_state.pending_input_for,
            get_execution_mode=lambda: self.execution_mode,
            threads=self.threads,
        )
        self.input_gate.set_toolbar_context_resolver(self.toolbar_coordinator.build_input_toolbar_context)
        self.input_gate.set_theme_cycle_handler(self.toolbar_coordinator.cycle_renderer_theme)
        self.chat_lifecycle = ChatLifecycle(
            chat_round_orchestrator=self.chat_round_orchestrator,
            system_layer=self.system_layer,
            renderer=self.renderer,
            runtime_state=self.runtime_state,
            turn_manager=self.turn_manager,
            agent_client=self.agent_client,
            ui_event_handler=self._ui_event_handler,
            session_services=self.session_services,
            task_services=self.task_services,
            session_state=self._chat_state,
            dispatch_services=self.dispatch_services,
            parse_routing=self.parse_routing,
            parse_response=self.parse_response,
            refresh_parallel_toolbar=self.toolbar_coordinator.refresh,
        )
        self.bug_services = BugServices(
            bug_store=self.bug_store,
            bug_detector=self.bug_detector,
            agent_bug_detector=self.agent_bug_detector,
            bug_correlator=self.bug_correlator,
            workspace=self.workspace,
            storage=self.storage,
            renderer=self.renderer,
            event_sink=self.event_sink,
            show_system_message=self.system_layer.show_system_message,
            show_warning_message=self.system_layer.show_warning_message,
            show_muted_message=self.system_layer.show_muted_message,
        )
        self.failure_tracker = AgentFailureTracker(
            normalize_agent_name=normalize_agent_name,
            agent_pool=self.agent_pool,
            release_agent_tasks=lambda name: runtime_tasks.release_agent_tasks(
                name, db_path=self.tasks_db_path
            ),
            record_metric=lambda name: self.session_metrics.record_agent_metric(
                self, name, "failed", 0
            ),
            file_bug=self._file_bug,
            get_session_id=lambda: getattr(self.storage, "session_id", ""),
        )
        self.command_router = CommandRouter(
            agent_pool=self.agent_pool,
            renderer=self.renderer,
            get_active_agent_plugins=lambda: self._plugin_resolver.active_plugins(self.agent_pool),
            set_execution_mode=self._set_execution_mode,
            normalize_agent_name=normalize_agent_name,
            selected_agents=self.selected_agents,
            get_available_plugins=lambda: self._plugin_resolver.plugins,
        )
        self._ui_subscriptions = self._ui_event_handler.wire_event_ui()

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
        chat_state = getattr(self, "_chat_state", None)
        if chat_state is not None:
            return chat_state.summary_agent_preference
        return self.__dict__.get("_summary_agent_preference_fallback")

    @summary_agent_preference.setter
    def summary_agent_preference(self, value):
        chat_state = getattr(self, "_chat_state", None)
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
            plugin = self._plugin_resolver.get(agent_name)
            if plugin and plugin.prefix:
                commands.add(plugin.prefix)
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
        if command == CMD_RESET:
            return ["state", "history", "all"]
        if command in ("s", "r"):
            return sorted(self.agent_pool)
        return []

    def _resolve_plugin_style(self, agent: str):
        """Resolve (color, label) para o agente; retorna None se não encontrado."""
        plugin = self._plugin_resolver.get(agent)
        return plugin.render_style if plugin else None

    def configure_mcp_socket(self, socket_path: str | None, token: str | None = None) -> None:
        """Propaga socket MCP e token para os plugins dos agentes ativos."""
        resolver = getattr(self, "_plugin_resolver", None)
        agent_pool = getattr(self, "agent_pool", None)
        if resolver is not None and agent_pool is not None:
            resolver.configure_mcp_socket(agent_pool, socket_path, token)
            return
        for plugin in self.get_active_agent_plugins():
            config_setter = getattr(plugin, "set_mcp_socket_config", None)
            if callable(config_setter):
                config_setter(socket_path, token)
            else:
                path_setter = getattr(plugin, "set_mcp_socket_path", None)
                if callable(path_setter):
                    path_setter(socket_path)

    def configure_mcp_http(self, url: str | None, token: str | None = None) -> None:
        """Propaga endpoint MCP HTTP e token para os plugins dos agentes ativos."""
        resolver = getattr(self, "_plugin_resolver", None)
        agent_pool = getattr(self, "agent_pool", None)
        if resolver is not None and agent_pool is not None:
            resolver.configure_mcp_http(agent_pool, url, token)
            return
        for plugin in self.get_active_agent_plugins():
            config_setter = getattr(plugin, "set_mcp_http_config", None)
            if callable(config_setter):
                config_setter(url, token)

    def get_agent_plugin(self, agent):
        """Retorna o plugin associado ao agente, ou None."""
        resolver = getattr(self, "_plugin_resolver", None)
        if resolver is not None:
            return resolver.get(agent)
        return plugins.get(agent)

    def get_available_plugins(self) -> list:
        """Retorna todos os plugins disponíveis."""
        resolver = getattr(self, "_plugin_resolver", None)
        if resolver is not None:
            return resolver.plugins
        return plugins.all_plugins()

    def get_active_agent_plugins(self) -> list:
        """Retorna plugins dos agentes ativos no pool."""
        resolver = getattr(self, "_plugin_resolver", None)
        if resolver is not None:
            return resolver.active_plugins(self.agent_pool)
        agent_pool = getattr(self, "agent_pool", None)
        if agent_pool is None:
            return []
        return [p for name in (agent_pool.agents or []) if (p := plugins.get(name)) is not None]

    def delegate(self, agent, **options):
        """Delega uma mensagem para o agente especificado."""
        return self.dispatch_services.delegate(agent, **options)

    def _refresh_parallel_toolbar(self) -> None:
        """Solicita redraw do prompt de paralelismo."""
        coordinator = getattr(self, "toolbar_coordinator", None)
        if coordinator is not None:
            coordinator.refresh()

    def _get_parallel_toolbar_state(self) -> dict:
        """Retorna cópia do estado de paralelismo da toolbar."""
        coordinator = getattr(self, "toolbar_coordinator", None)
        if coordinator is not None:
            return coordinator.get_parallel_toolbar_state()
        toolbar = getattr(self, "toolbar", None)
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
        coordinator = getattr(self, "toolbar_coordinator", None)
        if coordinator is not None:
            coordinator.set_parallel_toolbar_state(
                active=active,
                queued=queued,
                capacity=capacity,
                active_agents=active_agents,
            )

    def _resolve_active_model_label(self) -> str:
        """Resolve o modelo ativo para exibição na toolbar."""
        coordinator = getattr(self, "toolbar_coordinator", None)
        if coordinator is not None:
            return coordinator.resolve_active_model_label()
        return "unknown"

    def _resolve_next_responder_label(self) -> str:
        """Resolve o agente que responde na próxima rodada."""
        coordinator = getattr(self, "toolbar_coordinator", None)
        if coordinator is not None:
            return coordinator.resolve_next_responder_label()
        return "unknown"

    def _build_input_toolbar_context(self) -> dict:
        """Retorna contexto da toolbar do input."""
        coordinator = getattr(self, "toolbar_coordinator", None)
        if coordinator is not None:
            return coordinator.build_input_toolbar_context()
        return {}

    def _has_mcp_pending(self) -> bool:
        """Retorna True enquanto o MCP server interno tem tool calls em execução.

        Usado pelo ProcessRunner para suspender o idle timer do agente enquanto
        ele aguarda silenciosamente a resposta de uma tool call longa (ex: delegate).
        O atributo ``internal_mcp_server`` é setado por ``start_embedded_mcp`` após
        a criação do AgentClient, por isso o lookup é feito via getattr.
        """
        server = getattr(self, "internal_mcp_server", None)
        return bool(server and server.has_pending_calls)

    def __del__(self):
        """Libera recursos associados à instância."""
        try:
            self._stop_task_executors()
        except Exception:
            pass

    def record_success(self, agent):
        """Reseta o contador de falhas de um agente após resposta bem-sucedida."""
        tracker = getattr(self, "failure_tracker", None)
        if tracker is not None:
            tracker.record_success(agent)

    def record_failure(self, agent):
        """Registra failure e aplica política de remoção via AgentFailureTracker."""
        tracker = getattr(self, "failure_tracker", None)
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
        session_state = getattr(self, "session_state", {}) or {}
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

    def _setup_task_executors(self):
        """Set up task executors for explicit human-created task execution."""
        self.task_services.setup_task_executors()

    def _stop_task_executors(self):
        """Executa stop task executors."""
        self.task_services.stop_task_executors()

    def _make_ask_user_fn(self):
        """Cria callable que exibe seleção interativa e lê a resposta do usuário.

        Quando prompt_toolkit está ativo usa read_selection_in_terminal (setas +
        número). Caso contrário usa raw mode direto, com fallback readline para
        stdin não-tty.
        """
        import sys as _sys
        from .prompt_input import _raw_select

        input_gate = self.input_gate
        renderer = self.renderer

        def _ask_user(question: str, options: list) -> tuple:
            opts = [str(o) for o in options]
            gate_is_active = (
                input_gate is not None
                and callable(getattr(input_gate, "is_active", None))
                and input_gate.is_active()
            )
            if gate_is_active:
                result = input_gate.read_selection_in_terminal(question, opts)
                if result is not None:
                    return result
                raise EOFError("sem resposta do terminal")
            # Gate não ativo: raw mode com suspend/resume
            _suspend = getattr(renderer, "suspend_output", None)
            _resume = getattr(renderer, "resume_output", None)
            if callable(_suspend):
                _suspend()
            try:
                result = _raw_select(question, opts)
                if result is not None:
                    return result
                # stdin não é tty — fallback readline com loop de validação
                error_msg: str | None = None
                while True:
                    parts: list[str] = []
                    if error_msg:
                        parts.append(f"  ! {error_msg}")
                    parts.append(f"\n{question}")
                    for i, opt in enumerate(opts, 1):
                        parts.append(f"  {i}. {opt}")
                    parts.append(f"  (número 1-{len(opts)} ou texto exato)")
                    _sys.stdout.write("\n".join(parts) + "\n> ")
                    _sys.stdout.flush()
                    raw = _sys.stdin.readline().rstrip("\n\r").strip()
                    try:
                        idx = int(raw) - 1
                        if 0 <= idx < len(opts):
                            return idx, opts[idx]
                    except ValueError:
                        pass
                    for i, opt in enumerate(opts):
                        if opt.lower() == raw.lower():
                            return i, opt
                    error_msg = f"'{raw}' não é uma opção válida."
            finally:
                if callable(_resume):
                    _resume()

        return _ask_user

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
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        input_gate = getattr(self, "input_gate", None)
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
        stdout = sys.stdout
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

    def _set_execution_mode(self, mode):
        """Define o modo de execução ativo e propaga para policy e agent_client."""
        self.execution_mode = mode
        self.agent_client.execution_mode = mode
        if mode is not None:
            self.tool_executor.policy.blocked_tools = list(mode.blocked_tools)
        else:
            self.tool_executor.policy.blocked_tools = []

    def parse_routing(self, user_input: str) -> tuple[str | None, str | None, bool]:
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
        active_mode = getattr(getattr(self, "execution_mode", None), "name", None)
        return PromptFormatter.format_user_prompt(self.user_name, active_mode)

    def read_user_input(self, prompt, timeout: int):
        """Fachada compatível para leitura de input."""
        if not hasattr(self, "input_services") or self.input_services is None:
            return None
        return self.input_services.read_user_input(prompt, timeout)

    def _handle_bugs_command(self, command: str) -> bool:
        return self.bug_services.handle_bugs_command(
            command,
            app_session_state=getattr(self, "session_state", None)
        )

    def handle_command(self, user_input: str) -> bool:
        """Fachada compatível para comandos slash."""
        return self.system_layer.handle_command(user_input)

    def parse_response(self, response, **_kwargs):
        """Interpreta response."""
        protocol = self.protocol
        if getattr(protocol, "_shared_state", None) is not self.shared_state:
            sync_shared_state = getattr(protocol, "set_shared_state", None)
            if callable(sync_shared_state):
                sync_shared_state(self.shared_state)
        return self.protocol.parse_response(response)

    def _restore_current_job_env(self) -> None:
        """Restaura QUIMERA_CURRENT_JOB_ID para evitar vazamento entre sessões."""
        previous = getattr(self, "_previous_current_job_id_env", None)
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
