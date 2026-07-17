"""`AppAssembler`: monta os colaboradores de `QuimeraApp` em fases fixas.

Extraído do corpo de `QuimeraApp.__init__` (ver PLAN_APP_CORE_REFACTOR.md,
Fase 2). Nenhum comportamento muda: cada `_build_*` reproduz, na mesma
ordem, o trecho equivalente do `__init__` monolítico anterior; os binds
tardios que resolviam ciclos de construção (``bind_dispatch_services``,
``set_delegate_fn``, ``set_ask_user_fn`` etc.) foram concentrados em
``_wire``.
"""
import os
import platform as platform_info
import threading
import time

from .bundles import (
    AppBundles,
    ChatBundle,
    PlatformBundle,
    RuntimeBundle,
    SessionBundle,
    TaskBundle,
    UiBundle,
)
from .context import AppOptions
from ..agent_pool import AgentPool
from ..agent_run_events import AgentRunController
from ..agent_failure_tracker import AgentFailureTracker
from ..bug_services import BugServices
from ..chat_lifecycle import ChatLifecycle
from ..chat_round import ChatRoundOrchestrator
from ..command_router import CommandRouter
from ..config import logger, set_app_log_file
from ..dispatch import AppDispatchServices
from ..display_service import DisplayService
from ..event_sink import EventSink
from ..handlers import PromptAwareStderrHandler
from ..inputs import AppInputServices
from ..interfaces import ProfileResolverAdapter
from ..lifecycle import AppLifecycle
from ..protocol import AppProtocol
from ..session import AppSessionServices, compute_history_hard_limit, trim_history_messages
from ..session_bootstrap import (
    resolve_app_log_path,
    resolve_workspace_metrics_path,
    resolve_workspace_render_ansi_path,
    resolve_workspace_render_log_path,
)
from ..session_metrics import SessionMetricsService
from ..session_state import SessionStateManager
from ..staging import merge_staging_to_workspace
from ..state.session_state import SessionRuntimeState
from ..system_layer import AppSystemLayer
from ..toolbar import ToolbarManager
from ..toolbar_coordinator import ToolbarCoordinator
from ..turn import TurnManager
from ..ui_event_handler import UiEventHandler
from ...agents import AgentClient
from ...bugs import (
    AgentRuntimeBugDetector,
    BugCorrelator,
    BugStore,
    RenderBugDetector,
)
from ...config import ConfigManager
from ...constants import MSG_MIGRATION, Visibility
from ...context import ContextManager
from ...env_config import EnvConfig
from ...metrics import BehaviorMetricsTracker
from ...prompt import PromptBuilder
from ...runtime.input_broker import InputBroker
from ...runtime.process_supervisor import ProcessSupervisor
from ...runtime.workspace_policy import WorkspacePolicy
from ...session_summary import SessionSummarizer, build_chain_summarizer
from ...shared_state import bootstrap_state_key_stamps, clear_agent_state_for_session_start
from ...storage import SessionStorage
from ...tasks import api as runtime_tasks
from ...tasks.classifiers import classify_task_review_result
from ...tasks.executor import create_executor
from ...tasks.services import AppTaskServices
from ...ui import RenderAuditLogger, TerminalRenderer
from ...workspace import Workspace


def normalize_agent_name(agent):
    """Normaliza identificador de agente para nome canônico string."""
    if hasattr(agent, "name"):
        return getattr(agent, "name")
    return agent


def _make_record_agent_metric(session_metrics, app):
    def _fn(agent, metric, elapsed):
        session_metrics.record_agent_metric(app, agent, metric, elapsed)
    return _fn


def _make_record_tool_event(session_metrics, app):
    def _fn(agent, **kw):
        session_metrics.record_tool_event(app, agent, **kw)
    return _fn


def _make_background_delegate_fn(task_services, dispatch_services):
    def _fn(agent, **opts):
        bg = task_services._get_background_dispatch_services()
        return (bg or dispatch_services).delegate(agent, **opts)
    return _fn


def _make_active_profiles_fn(profile_resolver, agent_pool):
    def _fn():
        return profile_resolver.active_profiles(agent_pool)
    return _fn


def _make_record_metric(session_metrics, app):
    def _fn(name):
        session_metrics.record_agent_metric(app, name, "failed", 0)
    return _fn


def _make_get_session_id(storage):
    # getattr tardio: storages de teste são duck-typed e podem não expor
    # get_session_id(); preserva a semântica do getter original.
    def _fn():
        return getattr(storage, "session_id", "")
    return _fn


def _release_tasks_fn(tasks_db_path):
    def _fn(name):
        from ...tasks import api as _runtime_tasks
        _runtime_tasks.release_agent_tasks(name, db_path=tasks_db_path)
    return _fn


class AppAssembler:
    """Constrói os colaboradores de `QuimeraApp` em seis fases fixas."""

    def assemble(self, opts: AppOptions, app) -> AppBundles:
        """Monta `app` na ordem fixa platform → ui → session → runtime → tasks → chat → wire."""
        plat = self._build_platform(opts)
        self._apply_platform(app, plat)
        ui = self._build_ui(opts, app, plat)
        self._apply_ui(app, ui)
        sess = self._build_session(opts, app, plat, ui)
        self._apply_session(app, sess)
        rt = self._build_runtime(opts, app, plat, ui, sess)
        self._apply_runtime(app, rt)
        tasks = self._build_tasks(app, plat, ui, sess, rt)
        self._apply_tasks(app, tasks)
        chat = self._build_chat(app, plat, ui, sess, rt, tasks)
        self._apply_chat(app, chat)
        self._wire(app, plat, ui, sess, rt, tasks, chat)
        return AppBundles(plat, ui, sess, rt, tasks, chat)

    # ------------------------------------------------------------------
    # Fase 1: plataforma — workspace, config, storage, policy, bugs
    # ------------------------------------------------------------------

    def _build_platform(self, opts: AppOptions) -> PlatformBundle:
        lock = threading.Lock()
        output_lock = threading.Lock()
        counter_lock = threading.Lock()
        selected_agents = list(opts.agents) if opts.agents else []
        agent_pool = AgentPool(selected_agents)
        threads = int(opts.threads) if opts.threads is not None else 1
        toolbar = ToolbarManager(threads=threads)
        auto_approve_mutations = opts.auto_approve_mutations
        profile_registry = opts.profile_registry
        workspace = opts.workspace if opts.workspace is not None else Workspace(opts.cwd)
        EnvConfig(workspace.env_file).apply_to_environ()
        config = ConfigManager(workspace.config_file)
        workspace_policy_name = WorkspacePolicy.normalize_name(
            getattr(config, "workspace_policy", "strict")
        )
        workspace_policy = WorkspacePolicy.from_name(workspace_policy_name)
        active_theme = opts.theme if opts.theme is not None else config.theme
        storage = SessionStorage(workspace.logs_dir)
        session_started_at = time.monotonic()
        bug_store = BugStore(workspace.tmp.logs_dir)
        bug_detector = RenderBugDetector(repeat_threshold=2)
        agent_bug_detector = AgentRuntimeBugDetector()
        bug_correlator = BugCorrelator(window_seconds=60.0)
        session_id = storage.session_id
        render_log_path = resolve_workspace_render_log_path(workspace, session_id)
        render_ansi_path = resolve_workspace_render_ansi_path(workspace, session_id)
        metrics_file = resolve_workspace_metrics_path(workspace, session_id) if opts.debug else None
        app_log_path = resolve_app_log_path(workspace, session_id)
        if app_log_path:
            set_app_log_file(app_log_path)
        return PlatformBundle(
            lock=lock,
            output_lock=output_lock,
            counter_lock=counter_lock,
            selected_agents=selected_agents,
            agent_pool=agent_pool,
            threads=threads,
            toolbar=toolbar,
            auto_approve_mutations=auto_approve_mutations,
            profile_registry=profile_registry,
            workspace=workspace,
            config=config,
            workspace_policy_name=workspace_policy_name,
            workspace_policy=workspace_policy,
            active_theme=active_theme,
            storage=storage,
            session_started_at=session_started_at,
            bug_store=bug_store,
            bug_detector=bug_detector,
            agent_bug_detector=agent_bug_detector,
            bug_correlator=bug_correlator,
            session_id=session_id,
            render_log_path=render_log_path,
            render_ansi_path=render_ansi_path,
            metrics_file=metrics_file,
            app_log_path=app_log_path,
        )

    def _apply_platform(self, app, plat: PlatformBundle) -> None:
        app._lock = plat.lock
        app._output_lock = plat.output_lock
        app._counter_lock = plat.counter_lock
        app.selected_agents = plat.selected_agents
        app.agent_pool = plat.agent_pool
        app.threads = plat.threads
        app.toolbar = plat.toolbar
        app.auto_approve_mutations = plat.auto_approve_mutations
        app._profile_registry = plat.profile_registry
        app.workspace = plat.workspace
        app.config = plat.config
        app.workspace_policy_name = plat.workspace_policy_name
        app.workspace_policy = plat.workspace_policy
        app.storage = plat.storage
        app._session_started_at = plat.session_started_at
        app.bug_store = plat.bug_store
        app.bug_detector = plat.bug_detector
        app.agent_bug_detector = plat.agent_bug_detector
        app.bug_correlator = plat.bug_correlator

    # ------------------------------------------------------------------
    # Fase 2: UI — renderer, input gate/broker e canais de evento
    # ------------------------------------------------------------------

    def _build_ui(self, opts: AppOptions, app, plat: PlatformBundle) -> UiBundle:
        render_audit_logger = (
            RenderAuditLogger(plat.render_log_path, plat.render_ansi_path) if opts.debug else None
        )
        if opts.renderer_override is not None:
            renderer = opts.renderer_override
        else:
            renderer = TerminalRenderer(
                theme=plat.active_theme,
                get_profile_style=app._resolve_profile_style,
                density=plat.config.density,
                audit_logger=render_audit_logger,
            )
        agent_run_sink = AgentRunController(renderer)
        event_sink = EventSink()
        user_name = plat.config.user_name
        visibility = Visibility(opts.visibility)
        session_metrics = SessionMetricsService()
        # Placeholders lidos diretamente por AppTaskServices em
        # _build_tasks; precisam existir em `app` antes disso, tal como no
        # __init__ monolítico original — a morte desse padrão é a Fase 3.
        app.task_services = None
        app.task_executors = []
        app._approval_handler = None
        app.session_services = None
        app.execution_mode = None
        app.task_classifier = None
        app.tool_executor = None
        app.dispatch_services = None
        history_file = plat.workspace.history_file_for(plat.session_id)
        assert opts.input_gate_factory is not None, "input_gate_factory é obrigatório"
        input_gate = opts.input_gate_factory(
            renderer=renderer,
            history_file=history_file,
            command_resolver=app._available_commands,
            argument_resolver=app._command_argument_resolver,
        )
        input_broker = InputBroker(
            renderer=renderer,
            input_gate=input_gate,
            agent_run_sink=agent_run_sink,
        )
        return UiBundle(
            renderer=renderer,
            agent_run_sink=agent_run_sink,
            event_sink=event_sink,
            user_name=user_name,
            visibility=visibility,
            session_metrics=session_metrics,
            history_file=history_file,
            input_gate=input_gate,
            input_broker=input_broker,
        )

    def _apply_ui(self, app, ui: UiBundle) -> None:
        app.renderer = ui.renderer
        app.agent_run_sink = ui.agent_run_sink
        app.event_sink = ui.event_sink
        app.user_name = ui.user_name
        app.visibility = ui.visibility
        app.session_metrics = ui.session_metrics
        app.history_file = ui.history_file
        app.input_gate = ui.input_gate
        app.input_broker = ui.input_broker

    # ------------------------------------------------------------------
    # Fase 3: sessão — estado único de runtime, contexto e fachadas de UI
    # ------------------------------------------------------------------

    def _build_session(self, opts: AppOptions, app, plat: PlatformBundle, ui: UiBundle) -> SessionBundle:
        context_manager = ContextManager(
            plat.workspace.context_persistent,
            plat.workspace.context_session,
            ui.renderer,
            workspace=plat.workspace,
        )
        configured_history_window = opts.history_window or plat.config.history_window
        configured_auto_summarize_threshold = plat.config.auto_summarize_threshold
        history_hard_limit = compute_history_hard_limit(
            configured_history_window,
            configured_auto_summarize_threshold,
        )
        last_session = plat.storage.load_last_session()
        history, restored_drop_count = trim_history_messages(
            last_session["messages"],
            history_hard_limit,
        )
        if restored_drop_count:
            ui.renderer.show_system(
                f"[memória] histórico restaurado truncado para {len(history)} mensagens recentes\n"
            )
        session_runtime_state = SessionRuntimeState.from_legacy(
            history=history,
            shared_state=last_session["shared_state"],
        )
        session_state_mgr = SessionStateManager(
            storage=plat.storage,
            runtime_state=session_runtime_state,
        )
        shared_state = session_state_mgr.shared_state
        turn_stamps = session_state_mgr.turn_stamps
        shared_state_lock = session_state_mgr.shared_state_lock
        history_lock = session_state_mgr.history_lock
        history_restored = bool(history)
        clear_agent_state_for_session_start(shared_state, history_restored=history_restored)
        bootstrap_state_key_stamps(
            shared_state,
            turn_stamps,
            current_turn=int(shared_state.get("_current_turn", 0) or 0),
        )
        display_service = DisplayService(
            renderer=ui.renderer,
            input_status_getter=ui.input_gate.is_active,
            redisplay_prompt=app._redisplay_user_prompt_if_needed,
            output_lock=plat.output_lock,
            prompt_owner_thread_id_getter=ui.input_gate.get_owner_thread_id,
            run_above_active_prompt=ui.input_gate.run_in_terminal_message,
        )
        profile_resolver = ProfileResolverAdapter(
            registry=plat.profile_registry,
            normalize=normalize_agent_name,
        )
        system_layer = AppSystemLayer(
            display_service=display_service,
            profile_resolver=profile_resolver,
            prompt_builder=None,
            history_getter=session_state_mgr.history_snapshot,
            shared_state_getter=session_state_mgr.shared_state_snapshot,
            execution_mode_getter=app.execution_mode_state.get,
            agent_pool=plat.agent_pool,
            get_selected_agents=app.get_selected_agents,
            set_selected_agents=app.set_selected_agents,
            clear_screen=app.clear_terminal_screen,
            read_user_input=app.read_user_input,
            task_command_handler=None,
            bugs_command_handler=app._handle_bugs_command,
            session_state_manager=session_state_mgr,
            approval_handler_getter=app.get_approval_handler,
            context_manager=context_manager,
            profile_registry=plat.profile_registry,
            workspace_policy_getter=app.get_workspace_policy_name,
            workspace_policy_setter=app.set_workspace_policy_name,
        )
        input_services = AppInputServices(
            ui.renderer,
            input_resolver=app.resolve_input_gate,
            get_input_status=ui.input_gate.is_active,
            set_input_status=app.runtime_state.set_input_status,
            set_prompt_text=app.runtime_state.set_prompt_text,
            set_prompt_owner=app.runtime_state.set_prompt_owner,
            set_prompt_visible=app.runtime_state.set_prompt_visible,
            flush_deferred_messages=system_layer.flush_deferred_messages,
            output_lock=plat.output_lock,
        )
        ui.renderer.set_prompt_integration(
            is_active_fn=ui.input_gate.is_active,
            run_above_fn=ui.input_gate.run_in_terminal_message,
        )
        migrated = plat.workspace.migrate_from_legacy(opts.cwd)
        for item in migrated:
            ui.renderer.show_system(MSG_MIGRATION.format(item))
        return SessionBundle(
            context_manager=context_manager,
            configured_history_window=configured_history_window,
            configured_auto_summarize_threshold=configured_auto_summarize_threshold,
            history=history,
            history_restored=history_restored,
            session_runtime_state=session_runtime_state,
            session_state_mgr=session_state_mgr,
            shared_state=shared_state,
            turn_stamps=turn_stamps,
            shared_state_lock=shared_state_lock,
            history_lock=history_lock,
            display_service=display_service,
            profile_resolver=profile_resolver,
            system_layer=system_layer,
            input_services=input_services,
        )

    def _apply_session(self, app, sess: SessionBundle) -> None:
        app.context_manager = sess.context_manager
        app.history = sess.history
        app._session_runtime_state = sess.session_runtime_state
        app.session_state_mgr = sess.session_state_mgr
        app.shared_state = sess.shared_state
        app._turn_stamps = sess.turn_stamps
        app._shared_state_lock = sess.shared_state_lock
        app._history_lock = sess.history_lock
        app._display_service = sess.display_service
        app._profile_resolver = sess.profile_resolver
        app.system_layer = sess.system_layer
        app.input_services = sess.input_services

    # ------------------------------------------------------------------
    # Fase 4: runtime — agent client, protocolo e estado de rodada
    # ------------------------------------------------------------------

    def _build_runtime(
        self, opts: AppOptions, app, plat: PlatformBundle, ui: UiBundle, sess: SessionBundle
    ) -> RuntimeBundle:
        workspace_tmp = getattr(plat.workspace, "tmp", None)
        workspace_tmp_root = getattr(workspace_tmp, "root", None)
        idle_timeout_seconds = (
            opts.idle_timeout_seconds
            if opts.idle_timeout_seconds is not None
            else plat.config.idle_timeout_seconds
        )
        process_supervisor = ProcessSupervisor()
        agent_client = AgentClient(
            ui.renderer,
            metrics_file=plat.metrics_file,
            idle_timeout=idle_timeout_seconds,
            visibility=ui.visibility,
            working_dir=str(plat.workspace.cwd),
            error_reporter=sess.system_layer.show_error_message,
            muted_reporter=sess.system_layer.show_muted_message,
            session_id=plat.session_id,
            workspace_tmp_root=workspace_tmp_root,
            process_supervisor=process_supervisor,
            pause_idle_if=app._has_mcp_pending,
        )
        plat.agent_pool.set_freeze_hooks(
            on_freeze=agent_client.open_persistent_session,
            on_unfreeze=agent_client.close_persistent_session,
        )
        task_executor_factory = create_executor
        session_summarizer = SessionSummarizer(
            ui.renderer,
            summarizer_call=build_chain_summarizer(
                agent_client,
                plat.agent_pool.list_agents,
            ),
        )
        session_context = sess.context_manager.load_session()
        summary_loaded = sess.context_manager.SUMMARY_MARKER in session_context
        session_state = sess.session_runtime_state.session_state
        session_state.update({
            "session_id": plat.session_id,
            "history_count": len(sess.history),
            "history_restored": sess.history_restored,
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
        })
        # Persist metrics state to workspace so agents can resume with previous metrics
        metrics_state_path = plat.workspace.state_dir / "metrics_state.json"
        behavior_metrics = BehaviorMetricsTracker(storage_path=metrics_state_path)
        agent_client.tool_event_callback = app._record_tool_event
        debug_prompt_metrics = opts.debug
        chat_state = sess.session_runtime_state
        chat_state.summary_agent_preference = plat.agent_pool.primary
        protocol = AppProtocol(
            lock=sess.shared_state_lock,
            shared_state=sess.shared_state,
            workspace=plat.workspace,
            decisions_log_path=plat.workspace.decisions_log,
            turn_stamps=sess.turn_stamps,
        )
        runtime_state = app.runtime_state
        deferred_system_messages: list = []
        max_deferred_system_messages = 20
        turn_manager = TurnManager()
        for handler in logger.handlers:
            if isinstance(handler, PromptAwareStderrHandler):
                handler.bind_callbacks(
                    output_lock=plat.output_lock,
                    redisplay_prompt=app._redisplay_user_prompt_if_needed,
                    show_error=sess.system_layer.show_error_message,
                    show_warning=sess.system_layer.show_warning_message,
                    show_system=sess.system_layer.show_system_message,
                    show_muted=sess.system_layer.show_muted_message,
                    is_reading=ui.input_gate.is_active,
                    debug_enabled=app.is_debug_prompt_enabled,
                )
        is_new_session = not sess.history_restored and not summary_loaded

        # Unify tasks database path
        tasks_db_path = str(plat.workspace.tasks_db)
        runtime_tasks.init_db(tasks_db_path)
        current_job_id = runtime_tasks.add_job(f"Session {plat.session_id}", db_path=tasks_db_path)
        session_state["current_job_id"] = current_job_id
        previous_current_job_id_env = os.environ.get("QUIMERA_CURRENT_JOB_ID")
        os.environ["QUIMERA_CURRENT_JOB_ID"] = str(current_job_id)

        prompt_session_state = {
            "session_id": session_state["session_id"],
            "is_new_session": app._format_yes_no(is_new_session),
            "history_restored": app._format_yes_no(sess.history_restored),
            "summary_loaded": app._format_yes_no(summary_loaded),
            "current_job_id": current_job_id,
            "workspace_root": str(plat.workspace.cwd),
            "workspace_data_root": str(plat.workspace.root / "data"),
            "workspace_tmp_root": str(workspace_tmp_root) if workspace_tmp_root is not None else "",
            "current_dir": ".",
            "os_info": f"{platform_info.system()} {platform_info.release()}",
            "render_debug_active": opts.debug,
            "render_log_path": str(plat.render_log_path) if opts.debug else "",
            "render_ansi_path": str(plat.render_ansi_path) if opts.debug else "",
            "metrics_path": str(plat.metrics_file) if plat.metrics_file else "",
            "app_log_path": str(plat.app_log_path) if plat.app_log_path else "",
            "mcp_enabled": False,
            "mcp_socket_path": "",
        }
        prompt_builder = PromptBuilder(
            sess.context_manager,
            history_window=sess.configured_history_window,
            session_state=prompt_session_state,
            user_name=ui.user_name,
            active_agents=plat.agent_pool.agents,
            active_agents_provider=plat.agent_pool.list_agents,
            orchestrator_provider=plat.agent_pool.get_orchestrator,
            metrics_tracker=behavior_metrics,
        )
        sess.system_layer._prompt_builder = prompt_builder
        auto_summarize_threshold = sess.configured_auto_summarize_threshold
        return RuntimeBundle(
            workspace_tmp_root=workspace_tmp_root,
            idle_timeout_seconds=idle_timeout_seconds,
            process_supervisor=process_supervisor,
            agent_client=agent_client,
            task_executor_factory=task_executor_factory,
            session_summarizer=session_summarizer,
            summary_loaded=summary_loaded,
            session_state=session_state,
            behavior_metrics=behavior_metrics,
            debug_prompt_metrics=debug_prompt_metrics,
            chat_state=chat_state,
            protocol=protocol,
            runtime_state=runtime_state,
            deferred_system_messages=deferred_system_messages,
            max_deferred_system_messages=max_deferred_system_messages,
            turn_manager=turn_manager,
            is_new_session=is_new_session,
            tasks_db_path=tasks_db_path,
            current_job_id=current_job_id,
            previous_current_job_id_env=previous_current_job_id_env,
            prompt_builder=prompt_builder,
            auto_summarize_threshold=auto_summarize_threshold,
        )

    def _apply_runtime(self, app, rt: RuntimeBundle) -> None:
        app.idle_timeout_seconds = rt.idle_timeout_seconds
        app.process_supervisor = rt.process_supervisor
        app.agent_client = rt.agent_client
        app.task_executor_factory = rt.task_executor_factory
        app.session_summarizer = rt.session_summarizer
        app.session_state = rt.session_state
        app.behavior_metrics = rt.behavior_metrics
        app.debug_prompt_metrics = rt.debug_prompt_metrics
        app._chat_state = rt.chat_state
        app.protocol = rt.protocol
        app.runtime_state = rt.runtime_state
        app._deferred_system_messages = rt.deferred_system_messages
        app._MAX_DEFERRED_SYSTEM_MESSAGES = rt.max_deferred_system_messages
        app.turn_manager = rt.turn_manager
        app.tasks_db_path = rt.tasks_db_path
        app.current_job_id = rt.current_job_id
        app._previous_current_job_id_env = rt.previous_current_job_id_env
        app.prompt_builder = rt.prompt_builder
        app.auto_summarize_threshold = rt.auto_summarize_threshold

    # ------------------------------------------------------------------
    # Fase 5: tasks — serviços de execução/despacho (sem os binds tardios)
    # ------------------------------------------------------------------

    def _build_tasks(
        self, app, plat: PlatformBundle, ui: UiBundle, sess: SessionBundle, rt: RuntimeBundle
    ) -> TaskBundle:
        task_services = AppTaskServices(
            task_executor_factory=rt.task_executor_factory,
            current_job_id=rt.current_job_id,
            agent_pool=plat.agent_pool,
            task_executors=app.task_executors,
            renderer=ui.renderer,
            input_services=sess.input_services,
            input_gate=ui.input_gate,
            event_sink=ui.event_sink,
            agent_run_sink=ui.agent_run_sink,
            agent_client=rt.agent_client,
            workspace=plat.workspace,
            get_dispatch_tool_executor=app.get_dispatch_tool_executor,
            get_dispatch_services=app.get_dispatch_services,
            auto_approve_mutations=plat.auto_approve_mutations,
            approval_handler=app._approval_handler,
            set_approval_handler=app.set_approval_handler,
            get_agent_profile=sess.profile_resolver.get,
            available_profiles=sess.profile_resolver.profiles,
            session_state=rt.chat_state,
            system_layer=sess.system_layer,
            task_classifier=app.task_classifier,
            user_name=ui.user_name,
            prompt_builder=rt.prompt_builder,
            visibility=ui.visibility,
            show_error_message=sess.system_layer.show_error_message,
            show_muted_message=sess.system_layer.show_muted_message,
            get_execution_mode=app.execution_mode_state.get,
            record_tool_event=app._record_tool_event,
            record_failure=app.record_failure,
            session_metrics=ui.session_metrics,
            get_debug_prompt_metrics=app.is_debug_prompt_enabled,
            get_workspace_policy=app.get_workspace_policy_ref,
            redisplay_prompt=app._redisplay_user_prompt_if_needed,
            output_lock=plat.output_lock,
            counter_lock=plat.counter_lock,
            get_session_services=app.get_session_services_ref,
            max_retries=app.MAX_RETRIES,
            retry_backoff_seconds=app.RETRY_BACKOFF_SECONDS,
            rate_limit_backoff_seconds=app.RATE_LIMIT_BACKOFF_SECONDS,
            parse_response=app.parse_response,
            classify_task_execution_result=app.classify_task_execution_result,
            classify_task_review_result=classify_task_review_result,
        )
        session_services = AppSessionServices(
            session_state=rt.chat_state,
            storage=plat.storage,
            renderer=ui.renderer,
            agent_pool=plat.agent_pool,
            context_manager=sess.context_manager,
            session_summarizer=rt.session_summarizer,
            task_services=task_services,
            prompt_builder=rt.prompt_builder,
            auto_summarize_threshold=rt.auto_summarize_threshold,
            summary_agent_preference=app.summary_agent_preference,
            agent_client=rt.agent_client,
        )
        dispatch_services = AppDispatchServices(
            prompt_builder=rt.prompt_builder,
            renderer=ui.renderer,
            get_agent_profile=sess.profile_resolver.get,
            session_state=rt.chat_state,
            get_execution_mode=app.execution_mode_state.get,
            refresh_task_state=task_services.refresh_task_shared_state,
            debug_prompt_metrics=rt.debug_prompt_metrics,
            redisplay_prompt=app._redisplay_user_prompt_if_needed,
            output_lock=plat.output_lock,
            counter_lock=plat.counter_lock,
            print_response_fn=app.print_response,
            persist_message_fn=session_services.persist_message,
            record_session_metric=_make_record_agent_metric(ui.session_metrics, app),
            record_tool_event_fn=_make_record_tool_event(ui.session_metrics, app),
            notify_warning=sess.system_layer.show_warning_message,
            notify_retry=sess.system_layer.notify_agent_retry,
            notify_error=sess.system_layer.show_error_message,
            max_retries=app.MAX_RETRIES,
            retry_backoff=app.RETRY_BACKOFF_SECONDS,
            rate_limit_backoff=app.RATE_LIMIT_BACKOFF_SECONDS,
            record_failure=app.record_failure,
            record_success=app.record_success,
            get_agent_client=app.get_agent_client_ref,
            get_tool_executor=app.get_tool_executor,
            agent_run_sink=ui.agent_run_sink,
        )
        tool_executor = task_services.build_tool_executor(
            require_approval_for_mutations=not plat.auto_approve_mutations
        )
        return TaskBundle(
            task_services=task_services,
            session_services=session_services,
            dispatch_services=dispatch_services,
            tool_executor=tool_executor,
        )

    def _apply_tasks(self, app, tasks: TaskBundle) -> None:
        app.task_services = tasks.task_services
        app.session_services = tasks.session_services
        app.dispatch_services = tasks.dispatch_services
        app.tool_executor = tasks.tool_executor

    # ------------------------------------------------------------------
    # Fase 6: chat — orquestração de rodada, toolbar e serviços auxiliares
    # ------------------------------------------------------------------

    def _build_chat(
        self,
        app,
        plat: PlatformBundle,
        ui: UiBundle,
        sess: SessionBundle,
        rt: RuntimeBundle,
        tasks: TaskBundle,
    ) -> ChatBundle:
        chat_round_orchestrator = ChatRoundOrchestrator(
            dispatch_services=tasks.dispatch_services,
            parse_routing=app.parse_routing,
            agent_pool=plat.agent_pool,
            session_services=tasks.session_services,
            parse_response=app.parse_response,
            agent_client=rt.agent_client,
            turn_manager=rt.turn_manager,
            task_services=tasks.task_services,
            get_agent_profile=sess.profile_resolver.get,
            behavior_metrics=rt.behavior_metrics,
            threads=plat.threads,
            session_state=rt.chat_state,
            show_system_message=sess.system_layer.show_system_message,
            renderer=ui.renderer,
            merge_staging_to_workspace=merge_staging_to_workspace,
        )
        ui_event_handler = UiEventHandler(
            renderer=ui.renderer,
            input_gate=ui.input_gate,
            runtime_state=rt.runtime_state,
            system_layer=sess.system_layer,
            event_sink=ui.event_sink,
            show_muted_message=sess.system_layer.show_muted_message,
            show_system_message=sess.system_layer.show_system_message,
            show_warning_message=sess.system_layer.show_warning_message,
            show_error_message=sess.system_layer.show_error_message,
            redisplay_user_prompt=app._redisplay_user_prompt_if_needed,
            output_lock=plat.output_lock,
        )
        toolbar_coordinator = ToolbarCoordinator(
            toolbar_manager=plat.toolbar,
            agent_pool=plat.agent_pool,
            get_agent_profile=sess.profile_resolver.get,
            workspace=plat.workspace,
            get_history=app.get_history_ref,
            storage=plat.storage,
            bug_store=plat.bug_store,
            get_session_started_at=app.get_session_started_at_ref,
            renderer=ui.renderer,
            config=plat.config,
            runtime_state=rt.runtime_state,
            input_gate=ui.input_gate,
            get_execution_mode=app.execution_mode_state.get,
            threads=plat.threads,
        )
        chat_lifecycle = ChatLifecycle(
            chat_round_orchestrator=chat_round_orchestrator,
            system_layer=sess.system_layer,
            renderer=ui.renderer,
            runtime_state=rt.runtime_state,
            turn_manager=rt.turn_manager,
            agent_client=rt.agent_client,
            ui_event_handler=ui_event_handler,
            session_services=tasks.session_services,
            task_services=tasks.task_services,
            session_state=rt.chat_state,
            dispatch_services=tasks.dispatch_services,
            parse_routing=app.parse_routing,
            parse_response=app.parse_response,
            refresh_parallel_toolbar=toolbar_coordinator.refresh,
        )
        bug_services = BugServices(
            bug_store=plat.bug_store,
            bug_detector=plat.bug_detector,
            agent_bug_detector=plat.agent_bug_detector,
            bug_correlator=plat.bug_correlator,
            workspace=plat.workspace,
            storage=plat.storage,
            renderer=ui.renderer,
            event_sink=ui.event_sink,
            show_system_message=sess.system_layer.show_system_message,
            show_warning_message=sess.system_layer.show_warning_message,
            show_muted_message=sess.system_layer.show_muted_message,
        )
        failure_tracker = AgentFailureTracker(
            normalize_agent_name=normalize_agent_name,
            agent_pool=plat.agent_pool,
            release_agent_tasks=_release_tasks_fn(rt.tasks_db_path),
            record_metric=_make_record_metric(ui.session_metrics, app),
            file_bug=app._file_bug,
            get_session_id=_make_get_session_id(plat.storage),
            notify_warning=sess.system_layer.show_warning_message,
        )
        command_router = CommandRouter(
            agent_pool=plat.agent_pool,
            renderer=ui.renderer,
            get_active_agent_profiles=_make_active_profiles_fn(sess.profile_resolver, plat.agent_pool),
            set_execution_mode=app._set_execution_mode,
            normalize_agent_name=normalize_agent_name,
            selected_agents=plat.selected_agents,
            get_available_profiles=sess.profile_resolver.get_profiles_list,
        )
        return ChatBundle(
            chat_round_orchestrator=chat_round_orchestrator,
            ui_event_handler=ui_event_handler,
            toolbar_coordinator=toolbar_coordinator,
            chat_lifecycle=chat_lifecycle,
            bug_services=bug_services,
            failure_tracker=failure_tracker,
            command_router=command_router,
        )

    def _apply_chat(self, app, chat: ChatBundle) -> None:
        app.chat_round_orchestrator = chat.chat_round_orchestrator
        app._ui_event_handler = chat.ui_event_handler
        app.toolbar_coordinator = chat.toolbar_coordinator
        app.chat_lifecycle = chat.chat_lifecycle
        app.bug_services = chat.bug_services
        app.failure_tracker = chat.failure_tracker
        app.command_router = chat.command_router

    # ------------------------------------------------------------------
    # Wire: único ponto de resolução dos ciclos de construção
    # ------------------------------------------------------------------

    def _wire(
        self,
        app,
        plat: PlatformBundle,
        ui: UiBundle,
        sess: SessionBundle,
        rt: RuntimeBundle,
        tasks: TaskBundle,
        chat: ChatBundle,
    ) -> None:
        sess.system_layer.task_command_handler = tasks.task_services.handle_task_command
        tasks.task_services.bind_session_services(tasks.session_services)
        tasks.task_services.bind_dispatch_services(tasks.dispatch_services)
        tasks.task_services.bind_dispatch_tool_executor(tasks.tool_executor)
        tasks.task_services.bind_primary_approval_handler(app._approval_handler)
        # Conecta o InputBroker ao ApprovalManager para serializar
        # approval e ask_user na mesma fila com timeout e auto-resposta segura.
        handler = app._approval_handler
        set_broker = getattr(handler, "set_input_broker", None)
        if callable(set_broker):
            set_broker(ui.input_broker)
        # Injeta o executor nos drivers de API do agent_client.
        rt.agent_client.tool_executor = tasks.tool_executor
        rt.agent_client.bind_tool_preview_callback(tasks.tool_executor)
        tasks.tool_executor.set_delegate_fn(tasks.dispatch_services.delegate)
        # background_delegate_fn usa AgentClient isolado (cancel_event próprio),
        # impedindo que Ctrl+C no fluxo do chat cancele delegates assíncronos
        # e que o delegate assíncrono afete o fluxo principal.
        tasks.tool_executor.set_background_delegate_fn(
            _make_background_delegate_fn(tasks.task_services, tasks.dispatch_services)
        )
        tasks.tool_executor.set_active_agents_provider(plat.agent_pool.list_agents)
        tasks.tool_executor.set_orchestrator_provider(plat.agent_pool.get_orchestrator)
        tasks.tool_executor.set_cancel_checker(rt.agent_client.is_cancelled)
        tasks.tool_executor.set_agent_cleanup_callback(app._cleanup_sub_agent_stream)
        tasks.tool_executor.set_ask_user_fn(ui.input_broker.request_ask_user)
        tasks.tool_executor.set_update_state_fn(rt.protocol.apply_state_update)
        app.lifecycle = AppLifecycle(app)
        app.lifecycle.start()
        ui.input_gate.set_toolbar_context_resolver(chat.toolbar_coordinator.build_input_toolbar_context)
        ui.input_gate.set_theme_cycle_handler(chat.toolbar_coordinator.cycle_renderer_theme)
        app._ui_subscriptions = chat.ui_event_handler.wire_event_ui()
