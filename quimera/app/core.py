"""Componentes de `quimera.app.core`."""
import inspect
import json
import os
import platform
import queue
import random
import shutil
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from contextlib import nullcontext
from importlib import metadata
from pathlib import Path

from .agent_pool import AgentPool, AgentPoolView
from .handlers import PromptAwareStderrHandler
from ..domain.session_state import SessionState
from .chat_round import ChatRoundContext, ChatRoundOrchestrator
from .bootstrapper import AppBootstrapper
from .chat_processor import run_chat_loop
from .protocol import AppProtocol
from .render_event import RenderEvent
from .session import AppSessionServices, compute_history_hard_limit, trim_history_messages
from .session_bootstrap import (
    resolve_session_log_path,
    resolve_render_debug_log_path,
    resolve_workspace_render_log_path,
    resolve_workspace_render_ansi_path,
    resolve_workspace_metrics_path,
)
from .toolbar import ToolbarManager
from .session_metrics import SessionMetricsService
from .dispatch import AppDispatchServices
from .inputs import AppInputServices
from .interfaces import PluginResolverAdapter
from .prompt_input import InputGate
from .runtime_state import AppRuntimeState
from .task import AppTaskServices, create_executor
from .task_classifiers import classify_task_execution_result, classify_task_review_result, parse_task_command
from .display_service import DisplayService
from .system_layer import AppSystemLayer
from .turn import TurnManager
from .event_sink import EventSink
from .ui_event_handler import UiEventHandler
from .worker import ChatWorker
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
from ..runtime import tasks as runtime_tasks
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
    CMD_PROMPT, CMD_RELOAD, CMD_RESET_STATE, CMD_TASK,
    MSG_CHAT_STARTED, MSG_SESSION_LOG, MSG_SESSION_STATUS, MSG_MIGRATION,
    MSG_SHUTDOWN,
    Visibility,
)
from ..modes import MODES
from ..shared_state import bootstrap_state_key_stamps, clear_agent_state_for_session_start, expire_stale_keys
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

    def _ensure_runtime_state(self):
        try:
            return object.__getattribute__(self, "runtime_state")
        except AttributeError:
            rs = AppRuntimeState()
            object.__setattr__(self, "runtime_state", rs)
            return rs

    def _ensure_toolbar(self):
        try:
            return object.__getattribute__(self, "toolbar")
        except AttributeError:
            threads = getattr(self, "threads", 1)
            tb = ToolbarManager(threads=threads)
            legacy_state = getattr(self, "_parallel_toolbar_state", None)
            if legacy_state is not None:
                tb._parallel_toolbar_state.update(legacy_state)
            object.__setattr__(self, "toolbar", tb)
            return tb

    def __getattr__(self, name: str):
        if name == "runtime_state":
            return self._ensure_runtime_state()
        if name == "bug_services":
            return self._ensure_bug_services()
        if name == "command_router":
            return self._ensure_command_router()
        if name == "toolbar":
            return self._ensure_toolbar()
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name: str, value):
        object.__setattr__(self, name, value)

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
        self._lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._shared_state_lock = threading.Lock()
        self.selected_agents = list(agents) if agents else []
        self.agent_pool = AgentPool(self.selected_agents)
        self.threads = int(threads) if threads is not None else 1
        self.toolbar = ToolbarManager(threads=self.threads)
        self.agent_failures = defaultdict(int)
        self._agent_failures_lock = threading.Lock()
        self.auto_approve_mutations = auto_approve_mutations
        self._plugin_registry = plugin_registry
        infrastructure = AppBootstrapper(
            cwd,
            debug=debug,
            theme=theme,
            workspace=workspace,
            workspace_cls=Workspace,
            env_config_cls=EnvConfig,
            config_manager_cls=ConfigManager,
            session_storage_cls=SessionStorage,
            bug_store_cls=BugStore,
            render_bug_detector_cls=RenderBugDetector,
            agent_runtime_bug_detector_cls=AgentRuntimeBugDetector,
            bug_correlator_cls=BugCorrelator,
            render_audit_logger_cls=RenderAuditLogger,
            terminal_renderer_cls=TerminalRenderer,
            event_sink_cls=EventSink,
        ).build_infrastructure(
            get_plugin_style=self._resolve_plugin_style,
        )
        self.workspace = infrastructure.workspace
        self.config = infrastructure.config
        self.storage = infrastructure.storage
        self._session_started_at = time.monotonic()
        self.bug_store = infrastructure.bug_store
        self.bug_detector = infrastructure.bug_detector
        self.agent_bug_detector = infrastructure.agent_bug_detector
        self.bug_correlator = infrastructure.bug_correlator
        session_id = self.storage.session_id
        render_log_path = infrastructure.render_log_path
        render_ansi_path = infrastructure.render_ansi_path
        metrics_file = infrastructure.metrics_file
        self.renderer = infrastructure.renderer
        self.event_sink = infrastructure.event_sink
        self.user_name = self.config.user_name
        self.visibility = Visibility(visibility)
        self.session_metrics = SessionMetricsService()
        self.task_services = None
        self.task_executors = []
        self._approval_handler = None
        self.session_services = None
        self.system_layer = None
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
        self.input_services = AppInputServices(
            self.renderer,
            input_resolver=lambda: self.input_gate,
            get_input_status=self.input_gate.is_active,
            set_input_status=lambda v: setattr(self.runtime_state, 'nonblocking_input_status', v),
            set_prompt_text=lambda v: setattr(self.runtime_state, 'nonblocking_prompt_text', v),
            set_prompt_owner=lambda v: setattr(self.runtime_state, 'prompt_owning_thread_id', v),
            set_prompt_visible=lambda v: setattr(self.runtime_state, 'nonblocking_prompt_visible', v),
            flush_deferred_messages=lambda: self.system_layer.flush_deferred_messages(),
            output_lock=self._output_lock,
        )
        self.input_gate.set_toolbar_context_resolver(self._build_input_toolbar_context)
        self.input_gate.set_theme_cycle_handler(self._cycle_renderer_theme)
        self.renderer.set_prompt_integration(
            is_active_fn=self.input_gate.is_active,
            run_above_fn=self.input_gate.run_in_terminal_message,
        )
        migrated = self.workspace.migrate_from_legacy(cwd)
        for item in migrated:
            self.renderer.show_system(MSG_MIGRATION.format(item))

        self.context_manager = ContextManager(
            self.workspace.context_persistent,
            self.workspace.context_session,
            self.renderer,
            workspace=self.workspace,
        )
        workspace_tmp = getattr(self.workspace, "tmp", None)
        workspace_tmp_root = getattr(workspace_tmp, "root", None)
        self.agent_client = AgentClient(
            self.renderer,
            metrics_file=metrics_file,
            timeout=timeout,
            visibility=self.visibility,
            working_dir=str(self.workspace.cwd),
            error_reporter=self.show_error_message,
            muted_reporter=self.show_muted_message,
            session_id=session_id,
            workspace_tmp_root=workspace_tmp_root,
        )
        self.task_executor_factory = create_executor
        self.session_summarizer = SessionSummarizer(
            self.renderer,
            summarizer_call=build_chain_summarizer(
                self.agent_client,
                lambda: list(dict.fromkeys(self.agent_pool.agents)),
            ),
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
        self.shared_state = last_session["shared_state"]
        self._turn_stamps: dict = {}
        clear_agent_state_for_session_start(self.shared_state, history_restored=history_restored)
        bootstrap_state_key_stamps(
            self.shared_state,
            self._turn_stamps,
            current_turn=int(self.shared_state.get("_current_turn", 0) or 0),
        )
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
                    show_error=self.show_error_message,
                    show_warning=self.show_warning_message,
                    show_system=self.show_system_message,
                    show_muted=self.show_muted_message,
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
        self.auto_summarize_threshold = configured_auto_summarize_threshold
        self.task_services = AppTaskServices(
            task_executor_factory=self.task_executor_factory,
            get_current_job_id=lambda: self.current_job_id,
            get_agent_pool_agents=lambda: list(self.agent_pool.agents),
            get_task_executors=lambda: list(self.task_executors),
            set_task_executors=lambda executors: setattr(self, "task_executors", list(executors)),
            get_renderer=lambda: self.renderer,
            get_input_services=lambda: self.input_services,
            get_input_gate=lambda: self.input_gate,
            get_tasks_db_path=lambda: self.tasks_db_path,
            get_event_sink=lambda: self.event_sink,
            get_agent_client=lambda: self.agent_client,
            get_workspace=lambda: self.workspace,
            get_dispatch_tool_executor=lambda: self.tool_executor,
            get_dispatch_services=lambda: self.dispatch_services,
            get_auto_approve_mutations=lambda: self.auto_approve_mutations,
            get_approval_handler=lambda: self._approval_handler,
            set_approval_handler=lambda handler: setattr(self, "_approval_handler", handler),
            get_agent_plugin=self.get_agent_plugin,
            get_available_plugins=self.get_available_plugins,
            session_state=self._chat_state,
            get_system_layer=lambda: self.system_layer,
            get_task_classifier=lambda: self.task_classifier,
            get_user_name=lambda: self.user_name,
            get_prompt_builder=lambda: self.prompt_builder,
            get_visibility=lambda: self.visibility,
            get_show_error_message=lambda: self.show_error_message,
            get_show_muted_message=lambda: self.show_muted_message,
            get_execution_mode=lambda: self.execution_mode,
            get_record_tool_event=lambda: self._record_tool_event,
            get_record_failure=lambda: self.record_failure,
            get_session_metrics=lambda: self.session_metrics,
            get_debug_prompt_metrics=lambda: self.debug_prompt_metrics,
            get_redisplay_prompt=lambda: self._redisplay_user_prompt_if_needed,
            get_output_lock=lambda: self._output_lock,
            get_counter_lock=lambda: self._counter_lock,
            get_session_services=lambda: self.session_services,
            max_retries=self.MAX_RETRIES,
            retry_backoff_seconds=self.RETRY_BACKOFF_SECONDS,
            get_rate_limit_backoff_seconds=lambda: getattr(self, 'RATE_LIMIT_BACKOFF_SECONDS', 30),
            call_agent=self.call_agent,
            parse_response=self.parse_response,
            classify_task_execution_result=self.classify_task_execution_result,
            classify_task_review_result=classify_task_review_result,
        )
        self.session_services = AppSessionServices(
            history=self.history,
            storage=self.storage,
            renderer=self.renderer,
            agent_pool=self.agent_pool,
            lock=self._lock,
            context_manager=self.context_manager,
            session_summarizer=self.session_summarizer,
            task_services=self.task_services,
            prompt_builder=self.prompt_builder,
            shared_state=self.shared_state,
            auto_summarize_threshold=self.auto_summarize_threshold,
            summary_agent_preference=self.summary_agent_preference,
            agent_client=self.agent_client,
        )
        self.dispatch_services = AppDispatchServices(
            prompt_builder=self.prompt_builder,
            renderer=self.renderer,
            get_agent_plugin=self.get_agent_plugin,
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
        self.chat_round_orchestrator = ChatRoundOrchestrator(
            dispatch_services=self.dispatch_services,
            parse_routing=self.parse_routing,
            agent_pool=self.agent_pool,
            session_services=self.session_services,
            parse_response=self.parse_response,
            agent_client=self.agent_client,
            turn_manager=self.turn_manager,
            task_services=self.task_services,
            get_agent_plugin=self.get_agent_plugin,
            behavior_metrics=self.behavior_metrics,
            threads=self.threads,
            session_state=self._chat_state,
            show_system_message=self.show_system_message,
            renderer=self.renderer,
            merge_staging_to_workspace=self._merge_staging_to_workspace,
        )
        self.idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else self.config.idle_timeout_seconds

        self.tool_executor = self.task_services.build_tool_executor(require_approval_for_mutations=not self.auto_approve_mutations)
        # Injeta o executor nos drivers de API do agent_client.
        self.agent_client.tool_executor = self.tool_executor
        self.tool_executor.set_call_agent_fn(self.dispatch_services.call_agent)
        self.tool_executor.set_active_agents_provider(lambda: list(self.agent_pool.agents))
        self._display_service = DisplayService(
            renderer=self.renderer,
            input_status_getter=self.input_gate.is_active,
            redisplay_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=self._output_lock,
            prompt_owner_thread_id_getter=self.input_gate.get_owner_thread_id,
            run_above_active_prompt=self.input_gate.run_in_terminal_message,
        )
        self.system_layer = AppSystemLayer(
            display_service=self._display_service,
            plugin_resolver=PluginResolverAdapter(
                registry=self._plugin_registry,
                normalize=self._normalize_agent_name,
            ),
            prompt_builder=self.prompt_builder,
            history_getter=lambda: list(getattr(self, "history", []) or []),
            shared_state_getter=lambda: getattr(self, "shared_state", None),
            execution_mode_getter=lambda: getattr(self, "execution_mode", None),
            agent_pool=self.agent_pool,
            get_selected_agents=lambda: list(getattr(self, "selected_agents", []) or []),
            set_selected_agents=lambda agents: setattr(self, "selected_agents", list(agents)),
            clear_screen=self.clear_terminal_screen,
            read_user_input=self.read_user_input,
            task_command_handler=self.task_services.handle_task_command,
            bugs_command_handler=self._handle_bugs_command,
            reset_shared_state=self.reset_shared_state,
            approval_handler_getter=lambda: getattr(self, "_approval_handler", None),
            context_manager=self.context_manager,
            plugin_registry=self._plugin_registry,
        )
        # Set up task executors for autonomous task execution
        self._setup_task_executors()
        self._ui_event_handler = UiEventHandler(
            renderer=self.renderer,
            input_gate=self.input_gate,
            runtime_state=self.runtime_state,
            system_layer=self.system_layer,
            event_sink=self.event_sink,
            show_muted_message=self.show_muted_message,
            show_system_message=self.show_system_message,
            show_warning_message=self.show_warning_message,
            show_error_message=self.show_error_message,
            redisplay_user_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=self._output_lock,
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
            show_system_message=self.show_system_message,
            show_warning_message=self.show_warning_message,
            show_muted_message=self.show_muted_message,
        )
        self.command_router = CommandRouter(
            agent_pool=self.agent_pool,
            renderer=self.renderer,
            get_active_agent_plugins=self.get_active_agent_plugins,
            set_execution_mode=self._set_execution_mode,
            normalize_agent_name=self._normalize_agent_name,
            selected_agents=self.selected_agents,
            get_available_plugins=self.get_available_plugins,
        )
        self._ui_subscriptions = self._ui_event_handler.wire_event_ui()

    def _ensure_bug_services(self) -> BugServices:
        try:
            return object.__getattribute__(self, "bug_services")
        except AttributeError:
            bs = BugServices(
                bug_store=getattr(self, "bug_store", None),
                bug_detector=getattr(self, "bug_detector", None),
                agent_bug_detector=getattr(self, "agent_bug_detector", None),
                bug_correlator=getattr(self, "bug_correlator", None),
                workspace=getattr(self, "workspace", None),
                storage=getattr(self, "storage", None),
                renderer=getattr(self, "renderer", None),
                event_sink=getattr(self, "event_sink", None),
                show_system_message=self.show_system_message,
                show_warning_message=self.show_warning_message,
                show_muted_message=self.show_muted_message,
            )
            object.__setattr__(self, "bug_services", bs)
            return bs

    def _ensure_command_router(self) -> CommandRouter:
        try:
            return object.__getattribute__(self, "command_router")
        except AttributeError:
            cr = CommandRouter(
                agent_pool=self._ensure_agent_pool(),
                renderer=getattr(self, "renderer", None),
                get_active_agent_plugins=self.get_active_agent_plugins,
                set_execution_mode=self._set_execution_mode,
                normalize_agent_name=self._normalize_agent_name,
                selected_agents=getattr(self, "selected_agents", []),
                get_available_plugins=self.get_available_plugins,
            )
            object.__setattr__(self, "command_router", cr)
            return cr

    def _ensure_ui_event_handler(self):
        """Retorna _ui_event_handler, criando lazy se necessário (ex.: __new__ em testes)."""
        handler = getattr(self, '_ui_event_handler', None)
        if handler is not None:
            return handler
        handler = UiEventHandler(
            renderer=getattr(self, 'renderer', None),
            input_gate=getattr(self, 'input_gate', None),
            runtime_state=self._ensure_runtime_state(),
            system_layer=getattr(self, 'system_layer', None),
            event_sink=getattr(self, 'event_sink', None),
            show_muted_message=self.show_muted_message,
            show_system_message=self.show_system_message,
            show_warning_message=self.show_warning_message,
            show_error_message=self.show_error_message,
            redisplay_user_prompt=self._redisplay_user_prompt_if_needed,
            output_lock=getattr(self, '_output_lock', nullcontext()),
        )
        object.__setattr__(self, '_ui_event_handler', handler)
        return handler

    def _ensure_agent_pool(self) -> AgentPool:
        """Materializa o pool ao acessar instâncias criadas via ``__new__``."""
        pool = getattr(self, "agent_pool", None)
        if pool is None:
            pool = AgentPool([])
            self.agent_pool = pool
        return pool

    @property
    def active_agents(self):
        """Compatibilidade temporária com call sites legados baseados em lista."""
        return AgentPoolView(self._ensure_agent_pool())

    @active_agents.setter
    def active_agents(self, agents) -> None:
        self._ensure_agent_pool().set(list(agents or []))

    # ------------------------------------------------------------------
    # Propriedades que delegam para _chat_state (compatibilidade)
    # ------------------------------------------------------------------

    @property
    def round_index(self) -> int:
        cs = getattr(self, '_chat_state', None)
        return cs.round_index if cs is not None else getattr(self, '_round_index_raw', 0)

    @round_index.setter
    def round_index(self, value: int) -> None:
        cs = getattr(self, '_chat_state', None)
        if cs is not None:
            cs.round_index = value
        else:
            object.__setattr__(self, '_round_index_raw', value)

    @property
    def session_call_index(self) -> int:
        cs = getattr(self, '_chat_state', None)
        return cs.call_index if cs is not None else getattr(self, '_call_index_raw', 0)

    @session_call_index.setter
    def session_call_index(self, value: int) -> None:
        cs = getattr(self, '_chat_state', None)
        if cs is not None:
            cs._call_index = value
        else:
            object.__setattr__(self, '_call_index_raw', value)

    @property
    def summary_agent_preference(self) -> str | None:
        cs = getattr(self, '_chat_state', None)
        return cs.summary_agent_preference if cs is not None else getattr(self, '_summary_agent_pref_raw', None)

    @summary_agent_preference.setter
    def summary_agent_preference(self, value: str | None) -> None:
        cs = getattr(self, '_chat_state', None)
        if cs is not None:
            cs.summary_agent_preference = value
        else:
            object.__setattr__(self, '_summary_agent_pref_raw', value)

    @property
    def _pending_input_for(self) -> str | None:
        cs = getattr(self, '_chat_state', None)
        return cs.pending_input_for if cs is not None else getattr(self, '_pending_input_for_raw', None)

    @_pending_input_for.setter
    def _pending_input_for(self, value: str | None) -> None:
        cs = getattr(self, '_chat_state', None)
        if cs is not None:
            cs.pending_input_for = value
        else:
            object.__setattr__(self, '_pending_input_for_raw', value)

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
        for agent_name in self.agent_pool:
            plugin = self.get_agent_plugin(agent_name)
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
        return []

    def _resolve_plugin_style(self, agent: str):
        """Resolve (color, label) para o agente; retorna None se não encontrado."""
        plugin = self.get_agent_plugin(agent)
        return plugin.render_style if plugin else None

    @staticmethod
    def _normalize_agent_name(agent):
        return normalize_agent_name(agent)

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

    def configure_mcp_socket(self, socket_path: str | None, token: str | None = None) -> None:
        """Propaga o socket MCP e token para os plugins dos agentes ativos na sessão."""
        for plugin in self.get_active_agent_plugins():
            config_setter = getattr(plugin, "set_mcp_socket_config", None)
            if callable(config_setter):
                config_setter(socket_path, token)
            else:
                path_setter = getattr(plugin, "set_mcp_socket_path", None)
                if callable(path_setter):
                    path_setter(socket_path)

    def configure_mcp_http(self, url: str | None, token: str | None = None) -> None:
        """Compatibilidade legada: não usado no fluxo normal dos agentes locais.

        O MCP HTTP externo é apenas para clientes remotos; plugins CLI locais
        devem receber o MCP socket interno via ``configure_mcp_socket``.
        """
        for plugin in self.get_active_agent_plugins():
            config_setter = getattr(plugin, "set_mcp_http_config", None)
            if callable(config_setter):
                config_setter(url, token)

    def get_active_agent_plugins(self) -> list:
        """Retorna os plugins válidos dos agentes ativos na sessão."""
        active_plugins = []
        for agent_name in self.agent_pool:
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

    def record_success(self, agent):
        """Reseta o contador de falhas de um agente após resposta bem-sucedida."""
        agent_name = self._normalize_agent_name(agent)
        if not agent_name:
            return
        with self._agent_failures_lock:
            if self.agent_failures.get(agent_name, 0) > 0:
                self.agent_failures[agent_name] = 0
                logger.debug("agent %s failure counter reset after success", agent_name)

    def record_failure(self, agent):
        """Registra failure."""
        agent_name = self._normalize_agent_name(agent)
        if not agent_name:
            return
        with self._agent_failures_lock:
            self.agent_failures[agent_name] += 1
            failures = self.agent_failures[agent_name]
        if failures >= 2:
            if agent_name in self.agent_pool:
                self.agent_pool.remove(agent_name)
                logger.warning("agent %s removed after %d failures", agent_name, failures)
                try:
                    runtime_tasks.release_agent_tasks(agent_name, db_path=self.tasks_db_path)
                except Exception:
                    pass
        session_metrics = getattr(self, "session_metrics", None)
        if session_metrics is not None:
            session_metrics.record_agent_metric(self, agent_name, "failed", 0)
        # Emite bug apenas na transição para o estado de burst (2 falhas consecutivas),
        # evitando duplicação de log/evento para a mesma sequência contínua de falhas.
        if failures == 2:
            self._file_bug(
                session_id=getattr(self.storage, "session_id", ""),
                category="agent_failure_burst",
                summary=f"Agente {agent_name} acumulou falhas consecutivas",
                severity="medium",
                confidence=0.85,
                description=f"Falhas consecutivas atuais: {failures}",
                agent=agent_name,
            )

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

    def _wire_event_ui(self) -> None:
        """Conecta eventos de domínio à renderização UI."""
        handler = self._ensure_ui_event_handler()
        self._ui_subscriptions = handler.wire_event_ui()

    def _setup_task_executors(self):
        """Set up task executors for explicit human-created task execution."""
        self.task_services.setup_task_executors()

    def _stop_task_executors(self):
        """Executa stop task executors."""
        self.task_services.stop_task_executors()

    def _redisplay_user_prompt_if_needed(self, clear_first: bool = True) -> None:
        """Executa redisplay user prompt if needed."""
        _ = clear_first  # Mantido por compatibilidade de assinatura.
        stdin = sys.stdin
        if stdin is None or not stdin.isatty():
            return
        input_gate = getattr(self, "input_gate", None)
        is_active_fn = getattr(input_gate, "is_active", None)
        prompt_active = False
        if callable(is_active_fn):
            try:
                active_state = is_active_fn()
                if isinstance(active_state, bool):
                    prompt_active = active_state
            except Exception:
                prompt_active = False
        if not prompt_active:
            return
        try:
            redisplay = getattr(input_gate, "redisplay", None)
            if callable(redisplay):
                try:
                    redisplay()
                except Exception:
                    pass
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
            call_options = {"silent": silent, **dispatch_options}
            try:
                signature = inspect.signature(self._call_agent)
            except (TypeError, ValueError):
                filtered_options = call_options
            else:
                accepts_var_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
                if accepts_var_kwargs:
                    filtered_options = call_options
                else:
                    allowed = {
                        name
                        for name, parameter in signature.parameters.items()
                        if parameter.kind in (
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        )
                    }
                    filtered_options = {
                        key: value for key, value in call_options.items() if key in allowed
                    }
            response = self._call_agent(agent, **filtered_options)
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
            ver = metadata.version("quimera")
            if ver is not None:
                return ver
        except Exception:
            pass
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
        agent_name = self.agent_pool.primary
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
        if self.agent_pool.primary:
            return str(self.agent_pool.primary)
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
        }
        branch = getattr(workspace, "branch", None)
        if branch and isinstance(branch, str):
            ctx["branch"] = branch
        elapsed = time.monotonic() - getattr(self, "_session_started_at", time.monotonic())
        if elapsed >= 3600:
            mins = int(elapsed // 60)
            ctx["elapsed"] = f"{mins // 60}h {mins % 60:02d}m"
        elif elapsed >= 60:
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            ctx["elapsed"] = f"{mins}m {secs:02d}s"
        else:
            ctx["elapsed"] = f"{int(elapsed)}s"
        renderer = getattr(self, "renderer", None)
        theme_name = getattr(renderer, "theme_name", "") if renderer else ""
        ctx["theme"] = theme_name
        active_mode = getattr(self, "execution_mode", None)
        if active_mode is not None:
            ctx["mode"] = getattr(active_mode, "name", None) or ""
        parallel_state = self._get_parallel_toolbar_state()
        capacity = int(parallel_state.get("capacity", max(0, self.threads)) or 0)
        active = int(parallel_state.get("active", 0) or 0)
        queued = int(parallel_state.get("queued", 0) or 0)
        if active > 0 or queued > 0 or capacity > 1:
            slots_label = f"{active}/{capacity}"
            if queued:
                slots_label = f"{slots_label} · 📥 {queued}"
            ctx["parallel"] = slots_label
        active_agents = parallel_state.get("active_agents", ())
        if active_agents:
            normalized_agents = [str(a).strip() for a in active_agents if str(a).strip()]
            if normalized_agents:
                visible_agents = normalized_agents[:3]
                extra_agents = len(normalized_agents) - len(visible_agents)
                label = ", ".join(visible_agents)
                if extra_agents > 0:
                    label = f"{label} +{extra_agents}"
                ctx["active_agents"] = label
        history = getattr(self, "history", None)
        if history is not None:
            ctx["turns"] = str(len(history))
        # Add session ID to toolbar context
        session_id = getattr(getattr(self, "storage", None), "session_id", "")
        if session_id:
            ctx["session"] = session_id
        bug_store = getattr(self, "bug_store", None)
        if bug_store is not None:
            open_bug_count = None
            cache = getattr(self._ensure_toolbar(), "toolbar_bug_count_cache", None)
            cache_ttl = float(getattr(self._ensure_toolbar(), "toolbar_bug_count_ttl_sec", 1.0) or 1.0)
            now_monotonic = time.monotonic()
            if isinstance(cache, dict):
                cached_session = str(cache.get("session_id", ""))
                cached_ts = float(cache.get("ts", 0.0) or 0.0)
                if cached_session == str(session_id or "") and (now_monotonic - cached_ts) < cache_ttl:
                    cached_count = cache.get("count", 0)
                    try:
                        open_bug_count = int(cached_count)
                    except Exception:
                        open_bug_count = 0
            if open_bug_count is None:
                try:
                    open_bugs = bug_store.query(
                        session_id=session_id, status="open", limit=100
                    ) if session_id else bug_store.query(status="open", limit=100)
                    open_bug_count = len(open_bugs or [])
                    self._ensure_toolbar().toolbar_bug_count_cache = {
                        "session_id": str(session_id or ""),
                        "count": open_bug_count,
                        "ts": now_monotonic,
                    }
                except Exception:
                    open_bug_count = 0
            if open_bug_count > 0:
                ctx["open_bugs"] = str(open_bug_count)
        return ctx

    def _set_parallel_toolbar_state(
        self,
        *,
        active: int | None = None,
        queued: int | None = None,
        capacity: int | None = None,
        active_agents: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Atualiza o snapshot de paralelismo exibido na toolbar do prompt."""
        try:
            toolbar = object.__getattribute__(self, "toolbar")
        except AttributeError:
            toolbar = None
        setter = getattr(toolbar, "_set_parallel_toolbar_state", None) if toolbar is not None else None
        if callable(setter):
            setter(active=active, queued=queued, capacity=capacity, active_agents=active_agents)
            return

        with self._parallel_toolbar_lock:
            if active is not None:
                self._parallel_toolbar_state["active"] = max(0, int(active))
            if queued is not None:
                self._parallel_toolbar_state["queued"] = max(0, int(queued))
            if capacity is not None:
                self._parallel_toolbar_state["capacity"] = max(0, int(capacity))
            if active_agents is not None:
                self._parallel_toolbar_state["active_agents"] = tuple(active_agents)

    def _get_parallel_toolbar_state(self) -> dict[str, object]:
        """Retorna uma cópia do estado de paralelismo da toolbar.

        Usa ``_chat_inflight_count`` como fonte de verdade para slots ativos
        e deriva ``queued`` do tamanho da fila do chat quando disponível,
        garantindo que a toolbar reflita a ocupação real em runtime.
        """
        try:
            toolbar = object.__getattribute__(self, "toolbar")
        except AttributeError:
            toolbar = None
        getter = getattr(toolbar, "_get_parallel_toolbar_state", None) if toolbar is not None else None
        if callable(getter):
            snapshot = dict(getter() or {})
        else:
            with self._parallel_toolbar_lock:
                snapshot = dict(self._parallel_toolbar_state)
        active = self._get_chat_inflight_count()
        snapshot["active"] = active
        chat_queue = getattr(self.runtime_state, "chat_queue", None)
        if chat_queue is not None:
            try:
                queued_from_queue = max(0, int(chat_queue.qsize()))
                if queued_from_queue > 0:
                    snapshot["queued"] = queued_from_queue
            except Exception:
                pass
        return snapshot

    def _refresh_parallel_toolbar(self) -> None:
        """Solicita redraw do prompt quando o estado de paralelismo muda."""
        input_gate = getattr(self, "input_gate", None)
        redisplay = getattr(input_gate, "redisplay", None)
        if not callable(redisplay):
            return
        try:
            redisplay()
        except Exception:
            logger.debug("falha ao redesenhar toolbar de paralelismo", exc_info=True)

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
        orchestrator = self.chat_round_orchestrator
        ctx = ChatRoundContext(
            session_services=getattr(self, "session_services", None),
            task_services=getattr(self, "task_services", None),
            renderer=getattr(self, "renderer", None),
            session_state=getattr(self, "_chat_state", None),
            parse_routing=self.parse_routing,
            parse_response=self.parse_response,
            dispatch_services=getattr(self, "dispatch_services", None),
            show_system_message=getattr(self, "show_system_message", None),
            ui_queue=getattr(self, "_ui_event_queue", None),
        )
        orchestrator.process(user, ctx=ctx)

    def parse_response(self, response, **_kwargs):
        """Interpreta response."""
        if getattr(self.protocol, "_shared_state", None) is not self.shared_state:
            self.protocol._shared_state = self.shared_state
        return self.protocol.parse_response(response)

    def _advance_shared_state_turn(self) -> None:
        """Avança turno lógico de conversa e expira agent keys antigas."""
        shared = getattr(self, "shared_state", None)
        if not isinstance(shared, dict):
            return
        state_lock = getattr(self, "_shared_state_lock", None) or getattr(self, "_lock", None)
        if state_lock is None:
            return
        with state_lock:
            turn = int(shared.get("_current_turn", 0) or 0) + 1
            shared["_current_turn"] = turn
            stamps = getattr(self, "_turn_stamps", None)
            if isinstance(stamps, dict):
                expired = expire_stale_keys(shared, stamps, turn)
                if expired:
                    logger.info("[shared_state] expired stale keys: %s", expired)

    def reset_shared_state(self) -> None:
        """Limpa o shared_state em memória e persiste o snapshot atualizado."""
        state_lock = getattr(self, "_shared_state_lock", None) or getattr(self, "_lock", None)
        if state_lock is None:
            self.shared_state.clear()
            stamps = getattr(self, "_turn_stamps", None)
            if isinstance(stamps, dict):
                stamps.clear()
            self.storage.save_history(self.history, shared_state=self.shared_state)
            return
        with state_lock:
            self.shared_state.clear()
            stamps = getattr(self, "_turn_stamps", None)
            if isinstance(stamps, dict):
                stamps.clear()
            self.storage.save_history(self.history, shared_state=self.shared_state)

    def _merge_staging_to_workspace(self, staging_root: Path):
        """Mescla arquivos do staging para o workspace em ordem de índice com auditoria."""

        if not staging_root.exists():
            logger.debug("merge: staging_root does not exist, skipping")
            return []

        workspace_root = self.workspace.cwd.resolve()
        manifest = []
        index_dirs = sorted(staging_root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 999)

        for index_dir in index_dirs:
            if not index_dir.is_dir() or index_dir.is_symlink():
                continue
            for src in index_dir.rglob("*"):
                if src.is_symlink():
                    raise ValueError(f"merge blocked symlink source: {src}")
                if not src.is_file():
                    continue
                rel_path = src.relative_to(index_dir)
                dest = self.workspace.cwd / rel_path
                dest_resolved = dest.resolve(strict=False)
                if not dest_resolved.is_relative_to(workspace_root):
                    raise ValueError(f"merge blocked destination outside workspace: {rel_path}")
                if dest.exists() and dest.is_symlink():
                    raise ValueError(f"merge blocked symlink destination: {dest}")
                overwritten = dest.exists()
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": str(src),
                    "destination": str(dest_resolved),
                    "relative_path": rel_path.as_posix(),
                    "overwritten": overwritten,
                }
                manifest.append(entry)
                logger.info(
                    "merge %s: %s -> %s",
                    "overwrote" if overwritten else "created",
                    src,
                    dest_resolved,
                )

        manifest_path = getattr(self.workspace, "state_dir", None)
        if manifest and manifest_path is not None:
            manifest_file = manifest_path / "staging_merge_manifest.jsonl"
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            with manifest_file.open("a", encoding="utf-8") as fh:
                for entry in manifest:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("merge completed: %d files to %s", len(manifest), workspace_root)
        return manifest

    def _restore_current_job_env(self) -> None:
        """Restaura QUIMERA_CURRENT_JOB_ID para evitar vazamento entre sessões."""
        previous = getattr(self, "_previous_current_job_id_env", None)
        if previous is None:
            os.environ.pop("QUIMERA_CURRENT_JOB_ID", None)
        else:
            os.environ["QUIMERA_CURRENT_JOB_ID"] = previous

    def _process_chat_message(self, user):
        """Executa process chat message com controle de turno."""
        agent_client = getattr(self, "agent_client", None)
        if agent_client is not None:
            agent_client._user_cancelled = False
            cancel_event = getattr(agent_client, "_cancel_event", None)
            if cancel_event is not None:
                cancel_event.clear()
            reset_cancel_notices = getattr(agent_client, "reset_cancel_notices", None)
            if callable(reset_cancel_notices):
                reset_cancel_notices()
        try:
            self._do_process_chat_message(user)
        finally:
            if (
                hasattr(self, "turn_manager")
                and self.turn_manager.is_ai_turn
                and self._get_chat_inflight_count() <= 1
            ):
                self.turn_manager.next_turn()

    def _get_chat_inflight_count(self) -> int:
        return self.runtime_state.get_chat_inflight_count()

    def _increment_chat_inflight(self) -> int:
        return self.runtime_state.increment_chat_inflight(self._refresh_parallel_toolbar)

    def _decrement_chat_inflight(self) -> int:
        return self.runtime_state.decrement_chat_inflight(self._refresh_parallel_toolbar)

    def _release_chat_slot(self) -> None:
        self.runtime_state.release_chat_slot()

    def _should_render_ui_event_above_prompt(self) -> bool:
        """Retorna True quando há prompt ativo controlado por outra thread."""
        return self._ensure_ui_event_handler()._should_render_ui_event_above_prompt()

    def _run_ui_event_above_prompt(self, callback) -> bool:
        """Tenta renderizar callback acima do prompt ativo via InputGate."""
        return self._ensure_ui_event_handler()._run_ui_event_above_prompt(callback)

    def _handle_local_processing_interrupt(self) -> None:
        """Cancela só o processamento atual e devolve o chat ao input."""
        if hasattr(self, "turn_manager") and self.turn_manager is not None:
            self.turn_manager.reset()
        self.show_muted_message("[cancelado] pelo usuário")
        self._refresh_parallel_toolbar()

    def _process_async_chat_message(self, user):
        """Processa um prompt vindo da fila assíncrona e libera o slot ao final."""
        try:
            self._process_chat_message(user)
        finally:
            remaining = self._decrement_chat_inflight()
            self._release_chat_slot()
            if remaining == 0 and hasattr(self, "turn_manager") and self.turn_manager.is_ai_turn:
                self.turn_manager.next_turn()

    def _submit_async_chat_message(self, user):
        """Submete um prompt já reservado para a pool de execução do chat."""
        runtime_state = self._ensure_runtime_state()
        chat_executor = getattr(runtime_state, "chat_executor", None)
        if chat_executor is None:
            raise RuntimeError("chat executor não inicializado")
        try:
            chat_executor.submit(self._process_async_chat_message, user)
            self._refresh_parallel_toolbar()
        except Exception:
            self._decrement_chat_inflight()
            self._release_chat_slot()
            self._refresh_parallel_toolbar()
            raise

    def _process_sync_chat_message_with_slot(self, user):
        """Executa um prompt no thread principal ocupando um slot de concorrência."""
        runtime_state = self._ensure_runtime_state()
        slot_semaphore = getattr(runtime_state, "chat_slot_semaphore", None)
        if slot_semaphore is not None:
            slot_semaphore.acquire()
        self._increment_chat_inflight()
        try:
            self._process_chat_message(user)
        finally:
            self._decrement_chat_inflight()
            self._release_chat_slot()

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

    def _drain_ui_events(self, ui_queue: "queue.Queue") -> None:
        """Consome todos os RenderEvents pendentes na fila e chama renderer na main thread."""
        self._ensure_ui_event_handler().drain_ui_events(ui_queue)

    def run(self):
        """Executa o loop interativo do chat multiagente."""
        run_chat_loop(
            self,
            chat_worker_cls=ChatWorker,
            turn_manager_cls=TurnManager,
        )
