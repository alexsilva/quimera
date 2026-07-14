"""Pool de executores de tasks: lifecycle, dispatch de background e aprovação.

Extraído de ``AppTaskServices`` na Fase 4 da refatoração arquitetural
(PLAN_APP_CORE_REFACTOR.md). Agrupa responsabilidades de lifecycle dos
executores, dispatch assíncrono de background e gerenciamento de
aprovação automática de tools para tasks.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentClient
from ..runtime.approval import ApprovalManager
from ..runtime.config import ToolRuntimeConfig
from ..runtime.executor import ToolExecutor
from .approval_policy import TaskApprovalPolicy
from .executor import create_executor
from .execution import TaskExecutionService
from .failover import TaskFailoverPolicy
from .repository import TaskRepository
from .review import TaskReviewService
from ..runtime.tools.todo import TodoRegistry


_BACKGROUND_AGENT_TIMEOUT_SECONDS = 120


class _BackgroundMetricsView:
    """Contrato mínimo exigido por SessionMetricsService: expõe ``session_state``."""

    def __init__(self, get_session_state: Callable[[], Any]) -> None:
        self._get_session_state = get_session_state

    @property
    def session_state(self):
        return self._get_session_state()


class TaskExecutorPool:
    """Pool de executores de tasks: lifecycle, dispatch de background e aprovação.

    Encapsula a criação/parada de executores assíncronos, o dispatch de
    background isolado e o gerenciamento de aprovação automática de tools.
    """

    def __init__(
        self,
        *,
        task_executor_factory: Callable[..., Any] = create_executor,
        agent_pool: Any = None,
        get_active_agents: Callable[[], list[Any]] | None = None,
        workspace: Any = None,
        get_workspace: Callable[[], Any] | None = None,
        renderer: Any = None,
        get_renderer: Callable[[], Any] | None = None,
        input_services: Any = None,
        get_input_services: Callable[[], Any] | None = None,
        input_gate: Any = None,
        get_input_gate: Callable[[], Any] | None = None,
        event_sink: Any = None,
        get_event_sink: Callable[[], Any] | None = None,
        agent_run_sink: Any = None,
        get_agent_run_sink: Callable[[], Any] | None = None,
        agent_client: Any = None,
        get_agent_client: Callable[[], Any] | None = None,
        visibility: Any = None,
        get_visibility: Callable[[], Any] | None = None,
        auto_approve_mutations: bool = False,
        get_auto_approve_mutations: Callable[[], bool] | None = None,
        system_layer: Any = None,
        get_system_layer: Callable[[], Any] | None = None,
        get_dispatch_tool_executor: Callable[[], ToolExecutor | None] | None = None,
        get_dispatch_services: Callable[[], Any] | None = None,
        get_approval_handler: Callable[[], Any] | None = None,
        set_approval_handler: Callable[[Any], None] | None = None,
        get_agent_profile: Callable[[str], Any] | None = None,
        session_state: Any = None,
        get_session_state: Callable[[], Any] | None = None,
        get_execution_mode: Callable[[], Any] | None = None,
        record_tool_event: Callable[..., None] | None = None,
        get_record_tool_event: Callable[[], Callable[..., None] | None] | None = None,
        record_failure: Callable[[str], None] | None = None,
        get_record_failure: Callable[[], Callable[[str], None] | None] | None = None,
        show_muted_message: Callable[[str], None] | None = None,
        get_show_muted_message: Callable[[], Callable[[str], None] | None] | None = None,
        session_metrics: Any = None,
        get_session_metrics: Callable[[], Any] | None = None,
        get_debug_prompt_metrics: Callable[[], bool] | None = None,
        redisplay_prompt: Callable[[], None] | None = None,
        get_redisplay_prompt: Callable[[], Callable[[], None] | None] | None = None,
        output_lock: Any = None,
        get_output_lock: Callable[[], Any] | None = None,
        counter_lock: Any = None,
        get_counter_lock: Callable[[], Any] | None = None,
        shared_state_lock: Any = None,
        get_shared_state_lock: Callable[[], Any] | None = None,
        get_session_services: Callable[[], Any] | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: int = 1,
        get_rate_limit_backoff_seconds: Callable[[], int] | None = None,
        get_workspace_policy: Callable[[], Any] | None = None,
        parse_response: Callable[[Any], tuple[Any, Any, Any, Any, Any]] | None = None,
        classify_task_execution_result: Callable[..., Any] | None = None,
        classify_task_review_result: Callable[..., Any] | None = None,
        delegate: Callable[..., Any] | None = None,
        approval_owner_id: int | None = None,
        # Accessors from protocol service
        get_prompt_builder: Callable[[], Any] | None = None,
        get_history: Callable[[], Any] | None = None,
        get_shared_state: Callable[[], Any] | None = None,
    ) -> None:
        self._task_executor_factory = task_executor_factory
        self._agent_pool = agent_pool
        self._get_active_agents = get_active_agents
        self._workspace = workspace
        self._get_workspace = get_workspace
        self._renderer = renderer
        self._get_renderer = get_renderer
        self._input_services = input_services
        self._get_input_services = get_input_services
        self._input_gate = input_gate
        self._get_input_gate = get_input_gate
        self._event_sink = event_sink
        self._get_event_sink = get_event_sink
        self._agent_run_sink = agent_run_sink
        self._get_agent_run_sink = get_agent_run_sink
        self._agent_client = agent_client
        self._get_agent_client = get_agent_client
        self._visibility = visibility
        self._get_visibility = get_visibility
        self._auto_approve_mutations = auto_approve_mutations
        self._get_auto_approve_mutations = get_auto_approve_mutations
        self._system_layer = system_layer
        self._get_system_layer = get_system_layer
        self._get_dispatch_tool_executor = get_dispatch_tool_executor
        self._get_dispatch_services = get_dispatch_services
        self._get_approval_handler = get_approval_handler
        self._set_approval_handler = set_approval_handler
        self._get_agent_profile = get_agent_profile
        self._session_state = session_state
        self._get_session_state_fn = get_session_state
        self._get_execution_mode = get_execution_mode
        self._record_tool_event = record_tool_event
        self._get_record_tool_event = get_record_tool_event
        self._record_failure = record_failure
        self._get_record_failure = get_record_failure
        self._show_muted_message = show_muted_message
        self._get_show_muted_message = get_show_muted_message
        self._session_metrics = session_metrics
        self._get_session_metrics = get_session_metrics
        self._get_debug_prompt_metrics = get_debug_prompt_metrics
        self._redisplay_prompt = redisplay_prompt
        self._get_redisplay_prompt = get_redisplay_prompt
        self._output_lock = output_lock
        self._get_output_lock = get_output_lock
        self._counter_lock = counter_lock
        self._get_counter_lock = get_counter_lock
        self._shared_state_lock = shared_state_lock
        self._get_shared_state_lock = get_shared_state_lock
        self._get_session_services = get_session_services
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._get_rate_limit_backoff_seconds = get_rate_limit_backoff_seconds
        self._get_workspace_policy = get_workspace_policy
        self._parse_response = parse_response
        self._classify_task_execution_result = classify_task_execution_result
        self._classify_task_review_result = classify_task_review_result
        self._delegate = delegate
        self._get_prompt_builder_fn = get_prompt_builder
        self._get_history_fn = get_history
        self._get_shared_state_fn = get_shared_state
        self._get_session_state_fn = get_session_state
        self._approval_policy = TaskApprovalPolicy(
            get_approval_handler=self.get_approval_handler,
            owner_id=approval_owner_id if approval_owner_id is not None else id(self),
        )
        # Mutable state
        self._task_executors_ref: list[Any] | None = None
        self._task_executors_getter: Callable[[], list[Any]] | None = None
        self._task_executors_setter: Callable[[list[Any]], None] | None = None
        self._current_job_id_value: Any = None
        self._current_job_id_getter: Callable[[], Any] | None = None
        self._background_dispatch_services: Any = None
        self._background_tool_executor: ToolExecutor | None = None
        self._approval_handler: Any = None
        self._dispatch_tool_executor: ToolExecutor | None = None
        self._dispatch_services: Any = None

    # ── Lifecycle: setup/stop ──────────────────────────────────────────

    def setup_task_executors(self, claim_gate=None, *, task_executors=None, task_executors_getter=None, task_executors_setter=None, current_job_id=None, current_job_id_getter=None):
        """Inicializa executores assíncronos para tasks humanas."""
        self._task_executors_ref = task_executors
        self._task_executors_getter = task_executors_getter
        self._task_executors_setter = task_executors_setter
        self._current_job_id_value = current_job_id
        self._current_job_id_getter = current_job_id_getter
        failover_policy = self._build_task_failover_policy()
        task_execution_service = self._build_task_execution_service(failover_policy)
        task_review_service = self._build_task_review_service(failover_policy)
        repository = self._build_task_repository()
        job_id = self._current_job_id()
        executors = []
        for agent in self._agent_pool_agents():
            executor = self._task_executor_factory(
                agent,
                task_execution_service.handler_for(agent),
                job_id=job_id,
                repository=repository,
            )
            if hasattr(executor, "set_review_eligibility"):
                executor.set_review_eligibility(
                    lambda agent_name=agent: failover_policy.is_operational_review_agent(agent_name)
                )
            if agent in failover_policy.review_agents_for():
                executor.set_review_handler(task_review_service.handler_for(agent))
            if claim_gate is not None and hasattr(executor, "set_claim_gate"):
                executor.set_claim_gate(claim_gate)
            executor.start()
            executors.append(executor)
        self._replace_task_executors(executors)

    def stop_task_executors(self):
        """Interrompe todos os executores de tasks em segundo plano."""
        for executor in self._task_executors():
            try:
                executor.stop()
            except KeyboardInterrupt:
                pass
            except Exception:
                pass
        background_dispatch = self._background_dispatch_services
        if background_dispatch is not None:
            try:
                background_dispatch.close()
            except Exception:
                pass
        job_id = self._current_job_id()
        if job_id is not None:
            TodoRegistry.cleanup(job_id)

    # ── Tool executor building ─────────────────────────────────────────

    def build_tool_executor(
        self,
        require_approval_for_mutations: bool = True,
        *,
        register_as_primary: bool = True,
        allow_ask_user: bool = True,
    ) -> ToolExecutor:
        """Cria o executor de ferramentas com configuração padrão."""
        renderer = self.get_renderer()
        input_services = self.get_input_services()
        input_gate = self.get_input_gate()
        workspace = self.get_workspace()
        rt_config = ToolRuntimeConfig(
            workspace_root=workspace.cwd,
            db_path=workspace.tasks_db,
            memory_file=getattr(workspace, "memory_file", None),
            require_approval_for_mutations=require_approval_for_mutations,
            allow_ask_user=allow_ask_user,
            workspace_policy=self._get_workspace_policy() if self._get_workspace_policy else None,
        )
        approval_handler = ApprovalManager(
            rt_config,
            renderer=renderer,
            suspend_fn=input_services.suspend_nonblocking if input_services else None,
            resume_fn=input_services.resume_nonblocking if input_services else None,
            input_gate=input_gate,
        )
        if register_as_primary:
            self._approval_handler = approval_handler
            if self._set_approval_handler is not None:
                self._set_approval_handler(approval_handler)
        return ToolExecutor(
            config=rt_config,
            approval_handler=approval_handler,
        )

    # ── Parallel delegation ────────────────────────────────────────────

    def delegate_for_parallel(
        self,
        agent,
        delegation,
        protocol_mode,
        staging_root: Path,
        index: int,
        cancel_event: threading.Event | None = None,
    ):
        """Executa chamada de agente em paralelo com staging isolado por worker."""
        background_dispatch = self._create_background_dispatch_services(
            cancel_checker_override=(
                (lambda: bool(cancel_event and cancel_event.is_set()))
                if cancel_event is not None
                else self._background_was_user_cancelled
            ),
            cancel_event=cancel_event,
        )
        delegate = self._delegate_call()
        if background_dispatch is not None:
            delegate = background_dispatch.delegate
        try:
            return delegate_for_parallel_with_client(
                delegate,
                self._parse_response,
                agent,
                delegation,
                protocol_mode,
                staging_root,
                index,
            )
        finally:
            close = getattr(background_dispatch, "close", None)
            if callable(close):
                close()

    # ── Bind methods ───────────────────────────────────────────────────

    def bind_dispatch_services(self, dispatch_services) -> None:
        """Vincula serviços de dispatch ao bootstrap."""
        self._dispatch_services = dispatch_services

    def bind_dispatch_tool_executor(self, tool_executor: ToolExecutor | None) -> None:
        """Vincula o ToolExecutor primário após sua criação."""
        self._dispatch_tool_executor = tool_executor

    def bind_primary_approval_handler(self, approval_handler) -> None:
        """Vincula o approval handler primário após o bootstrap."""
        self._approval_handler = approval_handler

    # ── Accessor methods (used by proxy and facade) ────────────────────

    def get_prompt_builder(self):
        return self._get_prompt_builder_fn() if self._get_prompt_builder_fn else None

    def get_renderer(self):
        if self._renderer is not None:
            return self._renderer
        return self._get_renderer() if self._get_renderer else None

    def get_input_services(self):
        if self._input_services is not None:
            return self._input_services
        return self._get_input_services() if self._get_input_services else None

    def get_input_gate(self):
        if self._input_gate is not None:
            return self._input_gate
        return self._get_input_gate() if self._get_input_gate else None

    def get_event_sink(self):
        if self._event_sink is not None:
            return self._event_sink
        return self._get_event_sink() if self._get_event_sink else None

    def get_agent_run_sink(self):
        if self._agent_run_sink is not None:
            return self._agent_run_sink
        return self._get_agent_run_sink() if self._get_agent_run_sink else None

    def get_agent_profile(self, agent_name):
        return self._get_agent_profile(agent_name) if self._get_agent_profile else None

    def get_workspace(self):
        if self._workspace is not None:
            return self._workspace
        return self._get_workspace() if self._get_workspace else None

    def get_history(self):
        if self._session_state is not None:
            return self._session_state.history
        return self._get_history_fn() if self._get_history_fn else []

    def get_shared_state(self):
        if self._session_state is not None:
            return self._session_state.shared_state
        return self._get_shared_state_fn() if self._get_shared_state_fn else None

    def get_execution_mode(self):
        return self._get_execution_mode() if self._get_execution_mode else None

    def get_session_state(self):
        if self._session_state is not None:
            return self._session_state.session_meta
        return self._get_session_state_fn() if self._get_session_state_fn else None

    def get_record_failure(self):
        if self._record_failure is not None:
            return self._record_failure
        return self._get_record_failure() if self._get_record_failure else None

    def get_record_tool_event(self):
        if self._record_tool_event is not None:
            return self._record_tool_event
        return self._get_record_tool_event() if self._get_record_tool_event else None

    def get_show_muted_message(self):
        if self._show_muted_message is not None:
            return self._show_muted_message
        return self._get_show_muted_message() if self._get_show_muted_message else None

    def get_session_metrics(self):
        if self._session_metrics is not None:
            return self._session_metrics
        return self._get_session_metrics() if self._get_session_metrics else None

    def get_redisplay_prompt(self):
        if self._redisplay_prompt is not None:
            return self._redisplay_prompt
        return self._get_redisplay_prompt() if self._get_redisplay_prompt else None

    def get_output_lock(self):
        if self._output_lock is not None:
            return self._output_lock
        return self._get_output_lock() if self._get_output_lock else None

    def get_counter_lock(self):
        if self._counter_lock is not None:
            return self._counter_lock
        return self._get_counter_lock() if self._get_counter_lock else None

    def get_shared_state_lock(self):
        if self._shared_state_lock is not None:
            return self._shared_state_lock
        return self._get_shared_state_lock() if self._get_shared_state_lock else None

    def get_agent_client(self):
        if self._agent_client is not None:
            return self._agent_client
        return self._get_agent_client() if self._get_agent_client else None

    def get_visibility(self):
        if self._visibility is not None:
            return self._visibility
        return self._get_visibility() if self._get_visibility else None

    def get_system_layer(self):
        if self._system_layer is not None:
            return self._system_layer
        return self._get_system_layer() if self._get_system_layer else None

    def get_auto_approve_mutations(self) -> bool:
        if self._get_auto_approve_mutations is not None:
            return bool(self._get_auto_approve_mutations())
        return bool(self._auto_approve_mutations)

    def get_dispatch_services(self):
        if self._dispatch_services is not None:
            return self._dispatch_services
        return self._get_dispatch_services() if self._get_dispatch_services else None

    def get_approval_handler(self):
        if self._approval_handler is not None:
            return self._approval_handler
        if self._get_approval_handler:
            return self._get_approval_handler()
        return None

    # ── Internal methods ───────────────────────────────────────────────

    def _current_job_id(self):
        if self._current_job_id_value is not None:
            return self._current_job_id_value
        return self._current_job_id_getter() if self._current_job_id_getter else None

    def _agent_pool_agents(self) -> list[Any]:
        if self._get_active_agents is not None:
            return list(self._get_active_agents() or [])
        if self._agent_pool is not None:
            return list(getattr(self._agent_pool, "agents", []) or [])
        return []

    def _task_executors(self) -> list[Any]:
        if self._task_executors_ref is not None:
            return list(self._task_executors_ref)
        return list(self._task_executors_getter() or []) if self._task_executors_getter else []

    def _replace_task_executors(self, executors: list[Any]) -> None:
        if self._task_executors_ref is not None:
            self._task_executors_ref.clear()
            self._task_executors_ref.extend(executors)
            return
        if self._task_executors_setter is not None:
            self._task_executors_setter(executors)

    def _delegate_call(self):
        if self._delegate is not None:
            return self._delegate
        dispatch_services = self.get_dispatch_services()
        if dispatch_services is not None:
            return dispatch_services.delegate
        raise RuntimeError("TaskExecutorPool.dispatch_services não foi associado")

    def _was_user_cancelled(self) -> bool:
        agent_client = self.get_agent_client()
        return bool(agent_client and agent_client._user_cancelled)

    def _background_was_user_cancelled(self) -> bool:
        return False

    def _get_background_tool_executor(self) -> ToolExecutor | None:
        if self._background_tool_executor is None:
            if self.get_workspace() is None:
                return self._dispatch_tool_executor
            self._background_tool_executor = self.build_tool_executor(
                require_approval_for_mutations=not self.get_auto_approve_mutations(),
                register_as_primary=False,
                allow_ask_user=False,
            )
        return self._background_tool_executor

    def _create_background_dispatch_services(
        self,
        *,
        cancel_checker_override=None,
        cancel_event: threading.Event | None = None,
    ):
        from ..app.dispatch import AppDispatchServices
        renderer = self.get_renderer()
        workspace = self.get_workspace()
        if renderer is None or workspace is None:
            return self.get_dispatch_services()

        chat_agent_client = self.get_agent_client()
        if chat_agent_client is None:
            return self.get_dispatch_services()
        background_timeout = getattr(chat_agent_client, "idle_timeout", None)
        if background_timeout is None or not isinstance(background_timeout, (int, float)) or background_timeout <= 0:
            background_timeout = _BACKGROUND_AGENT_TIMEOUT_SECONDS
        _muted = self.get_show_muted_message()
        session_state = self.get_session_state()
        workspace_tmp = getattr(workspace, "tmp", None)
        workspace_tmp_root = getattr(workspace_tmp, "root", None)
        background_agent_client = AgentClient(
            renderer,
            idle_timeout=background_timeout,
            visibility=self.get_visibility(),
            working_dir=str(workspace.cwd),
            error_reporter=_muted,
            muted_reporter=_muted,
            session_id=session_state.get("session_id") if isinstance(session_state, dict) else None,
            workspace_tmp_root=workspace_tmp_root,
        )
        background_agent_client.execution_mode = self.get_execution_mode()
        background_agent_client.tool_event_callback = self.get_record_tool_event()
        background_agent_client.tool_executor = self._get_background_tool_executor()
        if cancel_event is not None:
            background_agent_client._cancel_event = cancel_event

        def _redisplay_prompt(**kw):
            callback = self.get_redisplay_prompt()
            if callable(callback):
                callback(**kw)

        get_session_services = self._get_session_services or (lambda: None)

        def _persist_message(agent, text):
            persist = getattr(get_session_services(), "persist_message", None)
            if callable(persist):
                persist(agent, text)

        metrics_view = _BackgroundMetricsView(self.get_session_state)

        def _record_session_metric(agent, metric, elapsed):
            record = getattr(self.get_session_metrics(), "record_agent_metric", None)
            if callable(record):
                record(metrics_view, agent, metric, elapsed)

        def _record_tool_event(agent, **kw):
            record = getattr(self.get_session_metrics(), "record_tool_event", None)
            if callable(record):
                record(metrics_view, agent, **kw)

        return AppDispatchServices(
            agent_client_override=background_agent_client,
            tool_executor_override=background_agent_client.tool_executor,
            cancel_checker_override=cancel_checker_override or self._background_was_user_cancelled,
            prompt_builder=self.get_prompt_builder,
            renderer=self.get_renderer,
            get_agent_profile=self.get_agent_profile,
            get_execution_mode=self.get_execution_mode,
            refresh_task_state=lambda: None,
            agent_run_sink=self.get_agent_run_sink,
            debug_prompt_metrics=self._get_debug_prompt_metrics or (lambda: False),
            redisplay_prompt=_redisplay_prompt,
            output_lock=self.get_output_lock,
            counter_lock=self.get_counter_lock,
            session_metrics=self.get_session_metrics,
            print_response_fn=self._background_print_response,
            persist_message_fn=_persist_message,
            record_session_metric=_record_session_metric,
            record_tool_event_fn=_record_tool_event,
            notify_warning=lambda message: None,
            notify_error=lambda message: None,
            max_retries=self._max_retries,
            retry_backoff=self._retry_backoff_seconds,
            rate_limit_backoff=self._get_rate_limit_backoff_seconds or (lambda: 30),
            record_failure=self.get_record_failure(),
        )

    def _background_print_response(self, agent, response):
        """Renderiza resposta de agente em background via dispatch da sessão."""
        dispatch_services = self.get_dispatch_services()
        if dispatch_services is not None:
            return dispatch_services.print_response(agent, response)
        renderer = self.get_renderer()
        if renderer is not None and response is not None:
            renderer.show_message(agent, response)
        return None

    def _get_background_dispatch_services(self):
        if self._background_dispatch_services is not None:
            return self._background_dispatch_services
        self._background_dispatch_services = self._create_background_dispatch_services()
        return self._background_dispatch_services

    # ── Internal builders ──────────────────────────────────────────────

    def _build_task_repository(self) -> TaskRepository:
        workspace = self.get_workspace()
        if workspace is None:
            raise ValueError("Workspace is required to access task repository")
        return TaskRepository(workspace.tasks_db, event_sink=self.get_event_sink())

    def _build_task_execution_service(self, failover_policy: TaskFailoverPolicy) -> TaskExecutionService:
        return TaskExecutionService(
            dispatch_services=self._get_background_dispatch_services(),
            system_layer=self.get_system_layer(),
            repository=self._build_task_repository(),
            failover_policy=failover_policy,
            classify_task_execution_result=self._classify_task_execution_result,
            was_user_cancelled=self._background_was_user_cancelled,
            record_failure=self.get_record_failure(),
            before_agent_call=lambda agent_name: self._enable_task_tool_auto_approval(
                agent_name,
                approval_handler=getattr(self._get_background_tool_executor(), "approval_handler", None),
            ),
            after_agent_call=lambda agent_name: self._disable_task_tool_auto_approval(
                agent_name,
                approval_handler=getattr(self._get_background_tool_executor(), "approval_handler", None),
            ),
        )

    def _build_task_review_service(self, failover_policy: TaskFailoverPolicy) -> TaskReviewService:
        return TaskReviewService(
            dispatch_services=self._get_background_dispatch_services(),
            system_layer=self.get_system_layer(),
            repository=self._build_task_repository(),
            failover_policy=failover_policy,
            classify_task_review_result=self._classify_task_review_result,
            was_user_cancelled=self._background_was_user_cancelled,
            event_sink=self.get_event_sink(),
        )

    def _build_task_failover_policy(self) -> TaskFailoverPolicy:
        return TaskFailoverPolicy(
            active_agents=self._agent_pool_agents,
            get_agent_profile=self._get_agent_profile or (lambda _name: None),
            repository=self._build_task_repository(),
        )

    # ── Auto-approval (delegated to TaskApprovalPolicy) ────────────────

    def _enable_task_tool_auto_approval(self, agent_name: str, approval_handler=None) -> None:
        self._approval_policy.enable(agent_name, approval_handler=approval_handler)

    def _disable_task_tool_auto_approval(self, agent_name: str, approval_handler=None) -> None:
        self._approval_policy.disable(agent_name, approval_handler=approval_handler)

    def _task_approval_handlers(self, approval_handler=None) -> list[Any]:
        return self._approval_policy._resolve_handlers(approval_handler)


def delegate_for_parallel_with_client(
    delegate: Callable[..., Any],
    parse_response: Callable[[Any], tuple[Any, ...]],
    agent,
    delegation,
    protocol_mode,
    staging_root: Path,
    index: int,
):
    """Executa chamada paralela de agente com staging isolado por thread."""
    from ..runtime.tools.files import set_staging_root

    set_staging_root(staging_root / str(index))
    try:
        raw = delegate(agent, delegation=delegation, primary=False, protocol_mode=protocol_mode, silent=True, show_output=False)
        response, _, _, extend, _ = parse_response(raw)
        return agent, response, extend
    finally:
        set_staging_root(None)
