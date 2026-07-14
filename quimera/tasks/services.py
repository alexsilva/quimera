"""Componentes do domínio `quimera.tasks`.

``AppTaskServices`` é o adaptador fino entre o ``/task`` (e demais operações
de task do ``QuimeraApp``) e o domínio de tasks. Na Fase 4 da refatoração
arquitetural, a lógica foi dividida em:

- ``TaskProtocolService``  – parsing, roteamento, prompts e overview
- ``TaskExecutorPool``     – lifecycle dos executores, dispatch de background
                             e aprovação automática

``AppTaskServices`` permanece como fachada que compõe os sub-serviços
e preserva a API externa existente.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from ..agents import AgentClient
from ..constants import TaskStatus
from ..runtime.approval import ApprovalManager
from ..runtime.config import ToolRuntimeConfig
from ..runtime.executor import ToolExecutor
from .executor import create_executor
from ..tasks.planning import classify_task
from ..app.config import logger
from ..app.dispatch import AppDispatchServices
from ..domain.session_state import SessionRuntimeState
from ..runtime.tools.todo import TodoRegistry
from .classifiers import classify_task_execution_result, classify_task_review_result, parse_task_command
from .execution import TaskExecutionService
from .executor_pool import TaskExecutorPool, _BACKGROUND_AGENT_TIMEOUT_SECONDS, delegate_for_parallel_with_client
from .failover import TaskFailoverPolicy
from .prompt import TaskPromptFactory
from .protocol import TaskProtocolService
from .repository import TaskRepository
from .review import TaskReviewService
from .router import TaskRouter
from .utils import build_completed_task_results


class AppTaskServices:
    """Fachada entre o ``QuimeraApp`` e o domínio de tasks.

    Na Fase 4, a lógica foi dividida em ``TaskProtocolService`` (roteamento,
    comando, prompts, overview) e ``TaskExecutorPool`` (lifecycle, background
    dispatch, aprovação). Esta classe preserva a API externa, delegando
    para os sub-serviços apropriados.
    """

    def __init__(
        self,
        *,
        task_executor_factory: Callable[..., Any] = create_executor,
        current_job_id: Any = None,
        get_current_job_id: Callable[[], Any] | None = None,
        agent_pool: Any = None,
        get_agent_pool_agents: Callable[[], list[Any]] | None = None,
        task_executors: list[Any] | None = None,
        get_task_executors: Callable[[], list[Any]] | None = None,
        set_task_executors: Callable[[list[Any]], None] | None = None,
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
        workspace: Any = None,
        get_workspace: Callable[[], Any] | None = None,
        dispatch_tool_executor: ToolExecutor | None = None,
        get_dispatch_tool_executor: Callable[[], ToolExecutor | None] | None = None,
        dispatch_services: AppDispatchServices | None = None,
        get_dispatch_services: Callable[[], AppDispatchServices | None] | None = None,
        auto_approve_mutations: bool | None = None,
        get_auto_approve_mutations: Callable[[], bool] | None = None,
        approval_handler: Any = None,
        get_approval_handler: Callable[[], Any] | None = None,
        set_approval_handler: Callable[[Any], None] | None = None,
        get_agent_profile: Callable[[str], Any],
        available_profiles: list[Any] | None = None,
        get_available_profiles: Callable[[], list[Any]] | None = None,
        session_state: SessionRuntimeState | None = None,
        get_session_state: Callable[[], dict[str, Any] | None] | None = None,
        get_history: Callable[[], Any] | None = None,
        get_shared_state: Callable[[], dict[str, Any] | None] | None = None,
        get_round_index: Callable[[], int] | None = None,
        get_shared_state_lock: Callable[[], Any] | None = None,
        system_layer: Any = None,
        get_system_layer: Callable[[], Any] = None,
        task_classifier: Any = None,
        get_task_classifier: Callable[[], Any] = None,
        user_name: str | None = None,
        get_user_name: Callable[[], str] = None,
        prompt_builder: Any = None,
        get_prompt_builder: Callable[[], Any] = None,
        visibility: Any = None,
        get_visibility: Callable[[], Any] = None,
        show_error_message: Callable[[str], None] | None = None,
        get_show_error_message: Callable[[], Callable[[str], None] | None] = None,
        show_muted_message: Callable[[str], None] | None = None,
        get_show_muted_message: Callable[[], Callable[[str], None] | None] = None,
        get_execution_mode: Callable[[], Any] = None,
        record_tool_event: Callable[..., None] | None = None,
        get_record_tool_event: Callable[[], Callable[..., None] | None] = None,
        record_failure: Callable[[str], None] | None = None,
        get_record_failure: Callable[[], Callable[[str], None] | None] = None,
        session_metrics: Any = None,
        get_session_metrics: Callable[[], Any] = None,
        debug_prompt_metrics: bool | None = None,
        get_debug_prompt_metrics: Callable[[], bool] = None,
        redisplay_prompt: Callable[[], None] | None = None,
        get_redisplay_prompt: Callable[[], Callable[[], None] | None] = None,
        output_lock: Any = None,
        get_output_lock: Callable[[], Any] = None,
        counter_lock: Any = None,
        get_counter_lock: Callable[[], Any] = None,
        session_services: Any = None,
        get_session_services: Callable[[], Any] = None,
        max_retries: int = 2,
        retry_backoff_seconds: int = 1,
        rate_limit_backoff_seconds: int | None = None,
        get_rate_limit_backoff_seconds: Callable[[], int] | None = None,
        get_workspace_policy: Callable[[], Any] | None = None,
        delegate: Callable[..., Any] | None = None,
        parse_response: Callable[[Any], tuple[Any, Any, Any, Any, Any]],
        classify_task_execution_result: Callable[[str | None], tuple[bool, str]] = classify_task_execution_result,
        classify_task_review_result: Callable[[str | None], tuple[bool, str, str]] = classify_task_review_result,
    ):
        # ── Store raw accessors for lazy resolution ─────────────────
        self._task_executor_factory = task_executor_factory
        self._current_job_id_value = current_job_id
        self._current_job_id_getter = get_current_job_id
        self._agent_pool = agent_pool
        self._agent_pool_agents_getter = get_agent_pool_agents
        self._task_executors_ref = task_executors
        self._task_executors_getter = get_task_executors
        self._task_executors_setter = set_task_executors
        self._renderer = renderer
        self._renderer_getter = get_renderer
        self._input_services = input_services
        self._input_services_getter = get_input_services
        self._input_gate = input_gate
        self._input_gate_getter = get_input_gate
        self._event_sink = event_sink
        self._event_sink_getter = get_event_sink
        self._agent_run_sink = agent_run_sink
        self._agent_run_sink_getter = get_agent_run_sink
        self._agent_client = agent_client
        self._agent_client_getter = get_agent_client
        self._workspace = workspace
        self._workspace_getter = get_workspace
        self._dispatch_tool_executor = dispatch_tool_executor
        self._dispatch_tool_executor_getter = get_dispatch_tool_executor
        self._dispatch_services = dispatch_services
        self._dispatch_services_getter = get_dispatch_services
        self._auto_approve_mutations = auto_approve_mutations
        self._auto_approve_mutations_getter = get_auto_approve_mutations
        self._approval_handler = approval_handler
        self._approval_handler_getter = get_approval_handler
        self._set_approval_handler = set_approval_handler
        self._get_agent_profile = get_agent_profile
        self._available_profiles = available_profiles
        self._available_profiles_getter = get_available_profiles
        self._session_state_obj = session_state
        self._get_session_state_fn = get_session_state
        self._get_history_fn = get_history
        self._get_shared_state_fn = get_shared_state
        self._get_round_index_fn = get_round_index
        self._get_shared_state_lock_fn = get_shared_state_lock
        self._system_layer = system_layer
        self._system_layer_getter = get_system_layer
        self._task_classifier = task_classifier
        self._task_classifier_getter = get_task_classifier
        self._user_name = user_name
        self._user_name_getter = get_user_name
        self._prompt_builder = prompt_builder
        self._prompt_builder_getter = get_prompt_builder
        self._visibility = visibility
        self._visibility_getter = get_visibility
        self._show_error_message = show_error_message
        self._show_error_message_getter = get_show_error_message
        self._show_muted_message = show_muted_message
        self._show_muted_message_getter = get_show_muted_message
        self._get_execution_mode = get_execution_mode
        self._record_tool_event = record_tool_event
        self._record_tool_event_getter = get_record_tool_event
        self._record_failure = record_failure
        self._record_failure_getter = get_record_failure
        self._session_metrics = session_metrics
        self._session_metrics_getter = get_session_metrics
        self._debug_prompt_metrics = debug_prompt_metrics
        self._debug_prompt_metrics_getter = get_debug_prompt_metrics
        self._redisplay_prompt = redisplay_prompt
        self._redisplay_prompt_getter = get_redisplay_prompt
        self._output_lock = output_lock
        self._output_lock_getter = get_output_lock
        self._counter_lock = counter_lock
        self._counter_lock_getter = get_counter_lock
        self._session_services = session_services
        self._session_services_getter = get_session_services
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds
        self._rate_limit_backoff_seconds_getter = get_rate_limit_backoff_seconds
        self._workspace_policy_getter = get_workspace_policy
        self._delegate = delegate
        self._parse_response = parse_response
        self._classify_task_execution_result = classify_task_execution_result
        self._classify_task_review_result = classify_task_review_result
        self._background_dispatch_services: AppDispatchServices | None = None
        self._background_tool_executor: ToolExecutor | None = None

        # ── Create sub-services ─────────────────────────────────────
        self._protocol = TaskProtocolService(
            workspace=workspace,
            get_workspace=self._get_workspace,
            agent_pool=agent_pool,
            get_active_agents=self._agent_pool_agents,
            profile_resolver=self._build_profile_resolver(),
            get_agent_profile=self._get_agent_profile,
            get_available_profiles=self._get_available_profiles,
            task_classifier=task_classifier,
            get_task_classifier=self._get_task_classifier,
            user_name=user_name,
            get_user_name=self._get_user_name,
            system_layer=system_layer,
            get_system_layer=self._get_system_layer,
            prompt_builder=prompt_builder,
            get_prompt_builder=self._get_prompt_builder,
            get_current_job_id=self._current_job_id,
            get_event_sink=self._get_event_sink,
            get_shared_state=self._get_shared_state,
            get_history=self._get_history,
            session_state=session_state,
            wake_executors=self._wake_task_executors,
            get_renderer=self._get_renderer,
        )
        self._executor_pool = TaskExecutorPool(
            task_executor_factory=task_executor_factory,
            agent_pool=agent_pool,
            get_active_agents=self._agent_pool_agents,
            workspace=workspace,
            get_workspace=self._get_workspace,
            renderer=renderer,
            get_renderer=self._get_renderer,
            input_services=input_services,
            get_input_services=self._get_input_services,
            input_gate=input_gate,
            get_input_gate=self._get_input_gate,
            event_sink=event_sink,
            get_event_sink=self._get_event_sink,
            agent_run_sink=agent_run_sink,
            get_agent_run_sink=self._get_agent_run_sink,
            agent_client=agent_client,
            get_agent_client=self._get_agent_client,
            visibility=visibility,
            get_visibility=self._get_visibility,
            auto_approve_mutations=bool(auto_approve_mutations),
            get_auto_approve_mutations=self._get_auto_approve_mutations,
            system_layer=system_layer,
            get_system_layer=self._get_system_layer,
            get_dispatch_tool_executor=self._get_dispatch_tool_executor,
            get_dispatch_services=self._get_dispatch_services,
            get_approval_handler=self._get_approval_handler,
            set_approval_handler=set_approval_handler,
            get_agent_profile=get_agent_profile,
            session_state=session_state,
            get_session_state=self._get_session_state,
            get_execution_mode=get_execution_mode,
            record_tool_event=record_tool_event,
            get_record_tool_event=self._get_record_tool_event,
            record_failure=record_failure,
            get_record_failure=self._get_record_failure,
            show_muted_message=show_muted_message,
            get_show_muted_message=self._get_show_muted_message,
            session_metrics=session_metrics,
            get_session_metrics=self._get_session_metrics,
            get_debug_prompt_metrics=self._get_debug_prompt_metrics,
            redisplay_prompt=redisplay_prompt,
            get_redisplay_prompt=self._get_redisplay_prompt,
            output_lock=output_lock,
            get_output_lock=self._get_output_lock,
            counter_lock=counter_lock,
            get_counter_lock=self._get_counter_lock,
            shared_state_lock=self._get_shared_state_lock(),
            get_shared_state_lock=self._get_shared_state_lock,
            get_session_services=self._get_session_services,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            get_rate_limit_backoff_seconds=self._get_rate_limit_backoff_seconds,
            get_workspace_policy=self._get_workspace_policy,
            parse_response=parse_response,
            classify_task_execution_result=classify_task_execution_result,
            classify_task_review_result=classify_task_review_result,
            delegate=self._delegate_call,
            approval_owner_id=id(self),
            get_prompt_builder=self._get_prompt_builder,
            get_history=self._get_history,
            get_shared_state=self._get_shared_state,
        )

    # ── Profile resolver builder ────────────────────────────────────

    def _build_profile_resolver(self):
        gp = self._get_agent_profile
        if gp is None:
            return None
        ap = self._available_profiles
        apg = self._available_profiles_getter

        class _Resolver:
            pass
        r = _Resolver()
        r.get = gp
        r.profiles = list(ap or []) if ap is not None else (list(apg() or []) if callable(apg) else [])
        return r

    def _wake_task_executors(self):
        for executor in self._task_executors():
            if hasattr(executor, "wake"):
                executor.wake()

    # ── SessionState accessors ──────────────────────────────────────

    def _get_session_state(self) -> dict | None:
        if isinstance(self._session_state_obj, SessionRuntimeState):
            return self._session_state_obj.session_state
        return self._get_session_state_fn() if self._get_session_state_fn else None

    def _current_job_id(self):
        if self._current_job_id_value is not None:
            return self._current_job_id_value
        return self._current_job_id_getter() if self._current_job_id_getter else None

    def _agent_pool_agents(self) -> list[Any]:
        if self._agent_pool is not None:
            return list(getattr(self._agent_pool, "agents", []) or [])
        return list(self._agent_pool_agents_getter() or []) if self._agent_pool_agents_getter else []

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

    def _get_history(self) -> Any:
        if isinstance(self._session_state_obj, SessionRuntimeState):
            return self._session_state_obj.history
        return self._get_history_fn() if self._get_history_fn else []

    def _get_shared_state(self) -> dict | None:
        if isinstance(self._session_state_obj, SessionRuntimeState):
            return self._session_state_obj.shared_state
        return self._get_shared_state_fn() if self._get_shared_state_fn else None

    def _get_round_index(self) -> int:
        if isinstance(self._session_state_obj, SessionRuntimeState):
            return self._session_state_obj.round_index
        return self._get_round_index_fn() if self._get_round_index_fn else 0

    def _get_shared_state_lock(self) -> Any:
        if isinstance(self._session_state_obj, SessionRuntimeState):
            return self._session_state_obj.shared_state_lock
        return self._get_shared_state_lock_fn() if self._get_shared_state_lock_fn else None

    def _get_renderer(self):
        if self._renderer is not None:
            return self._renderer
        return self._renderer_getter() if self._renderer_getter else None

    def _get_input_services(self):
        if self._input_services is not None:
            return self._input_services
        return self._input_services_getter() if self._input_services_getter else None

    def _get_input_gate(self):
        if self._input_gate is not None:
            return self._input_gate
        return self._input_gate_getter() if self._input_gate_getter else None

    def _get_event_sink(self):
        if self._event_sink is not None:
            return self._event_sink
        return self._event_sink_getter() if self._event_sink_getter else None

    def _get_agent_run_sink(self):
        if self._agent_run_sink is not None:
            return self._agent_run_sink
        return self._agent_run_sink_getter() if self._agent_run_sink_getter else None

    def _get_agent_client(self):
        if self._agent_client is not None:
            return self._agent_client
        return self._agent_client_getter() if self._agent_client_getter else None

    def _get_workspace(self):
        if self._workspace is not None:
            return self._workspace
        return self._workspace_getter() if self._workspace_getter else None

    def _get_dispatch_tool_executor(self):
        if self._dispatch_tool_executor is not None:
            return self._dispatch_tool_executor
        return self._dispatch_tool_executor_getter() if self._dispatch_tool_executor_getter else None

    def _get_dispatch_services(self):
        if self._dispatch_services is not None:
            return self._dispatch_services
        return self._dispatch_services_getter() if self._dispatch_services_getter else None

    def _get_auto_approve_mutations(self) -> bool:
        if self._auto_approve_mutations is not None:
            return self._auto_approve_mutations
        return bool(self._auto_approve_mutations_getter()) if self._auto_approve_mutations_getter else False

    def _get_approval_handler(self):
        if self._approval_handler is not None:
            return self._approval_handler
        return self._approval_handler_getter() if self._approval_handler_getter else None

    def _get_available_profiles(self) -> list[Any]:
        if self._available_profiles is not None:
            return list(self._available_profiles)
        return list(self._available_profiles_getter() or []) if self._available_profiles_getter else []

    def _delegate_call(self, *args, **kwargs):
        if self._delegate is not None:
            return self._delegate(*args, **kwargs)
        dispatch_services = self._get_dispatch_services()
        if dispatch_services is None:
            raise RuntimeError("AppTaskServices.dispatch_services não foi associado")
        return dispatch_services.delegate(*args, **kwargs)

    def _get_system_layer(self):
        if self._system_layer is not None:
            return self._system_layer
        return self._system_layer_getter() if self._system_layer_getter else None

    def _get_task_classifier(self):
        if self._task_classifier is not None:
            return self._task_classifier
        return self._task_classifier_getter() if self._task_classifier_getter else None

    def _get_user_name(self):
        if self._user_name is not None:
            return self._user_name
        return self._user_name_getter() if self._user_name_getter else None

    def _get_prompt_builder(self):
        if self._prompt_builder is not None:
            return self._prompt_builder
        return self._prompt_builder_getter() if self._prompt_builder_getter else None

    def _get_visibility(self):
        if self._visibility is not None:
            return self._visibility
        return self._visibility_getter() if self._visibility_getter else None

    def _get_show_muted_message(self):
        if self._show_muted_message is not None:
            return self._show_muted_message
        return self._show_muted_message_getter() if self._show_muted_message_getter else None

    def _get_show_error_message(self):
        if self._show_error_message is not None:
            return self._show_error_message
        return self._show_error_message_getter() if self._show_error_message_getter else None

    def _get_record_tool_event(self):
        if self._record_tool_event is not None:
            return self._record_tool_event
        return self._record_tool_event_getter() if self._record_tool_event_getter else None

    def _get_record_failure(self):
        if self._record_failure is not None:
            return self._record_failure
        return self._record_failure_getter() if self._record_failure_getter else None

    def _get_session_metrics(self):
        if self._session_metrics is not None:
            return self._session_metrics
        return self._session_metrics_getter() if self._session_metrics_getter else None

    def _get_debug_prompt_metrics(self):
        if self._debug_prompt_metrics is not None:
            return self._debug_prompt_metrics
        return bool(self._debug_prompt_metrics_getter()) if self._debug_prompt_metrics_getter else False

    def _get_redisplay_prompt(self):
        if self._redisplay_prompt is not None:
            return self._redisplay_prompt
        return self._redisplay_prompt_getter() if self._redisplay_prompt_getter else None

    def _get_output_lock(self):
        if self._output_lock is not None:
            return self._output_lock
        return self._output_lock_getter() if self._output_lock_getter else None

    def _get_counter_lock(self):
        if self._counter_lock is not None:
            return self._counter_lock
        return self._counter_lock_getter() if self._counter_lock_getter else None

    def _get_session_services(self):
        if self._session_services is not None:
            return self._session_services
        return self._session_services_getter() if self._session_services_getter else None

    def _get_rate_limit_backoff_seconds(self):
        if self._rate_limit_backoff_seconds is not None:
            return self._rate_limit_backoff_seconds
        return self._rate_limit_backoff_seconds_getter() if self._rate_limit_backoff_seconds_getter else 30

    def _get_workspace_policy(self):
        return self._workspace_policy_getter() if callable(self._workspace_policy_getter) else None

    def _was_user_cancelled(self) -> bool:
        agent_client = self._get_agent_client()
        return bool(agent_client and agent_client._user_cancelled)

    def _background_was_user_cancelled(self) -> bool:
        return False

    # ── Bind methods ───────────────────────────────────────────────

    def bind_dispatch_services(self, dispatch_services: AppDispatchServices | None) -> None:
        """Vincula serviços de dispatch ao bootstrap."""
        self._dispatch_services = dispatch_services
        self._executor_pool.bind_dispatch_services(dispatch_services)

    def bind_dispatch_tool_executor(self, tool_executor: ToolExecutor | None) -> None:
        """Vincula o ToolExecutor primário após sua criação."""
        self._dispatch_tool_executor = tool_executor
        self._executor_pool.bind_dispatch_tool_executor(tool_executor)

    def bind_session_services(self, session_services: Any) -> None:
        """Vincula serviços de sessão após o bootstrap."""
        self._session_services = session_services

    def bind_primary_approval_handler(self, approval_handler: Any) -> None:
        """Vincula o approval handler primário após o bootstrap."""
        self._approval_handler = approval_handler
        self._executor_pool.bind_primary_approval_handler(approval_handler)

    # ── Setup / bootstrap (delegates to TaskExecutorPool) ──────────

    def setup_task_executors(self, claim_gate=None):
        """Inicializa executores assíncronos para tasks humanas."""
        return self._executor_pool.setup_task_executors(
            claim_gate=claim_gate,
            task_executors=self._task_executors_ref,
            task_executors_getter=self._task_executors_getter,
            task_executors_setter=self._task_executors_setter,
            current_job_id=self._current_job_id_value,
            current_job_id_getter=self._current_job_id_getter,
        )

    def build_tool_executor(
        self,
        require_approval_for_mutations: bool = True,
        *,
        register_as_primary: bool = True,
        allow_ask_user: bool = True,
    ) -> ToolExecutor:
        """Cria o executor de ferramentas com configura padrão."""
        executor = self._executor_pool.build_tool_executor(
            require_approval_for_mutations=require_approval_for_mutations,
            register_as_primary=register_as_primary,
            allow_ask_user=allow_ask_user,
        )
        if register_as_primary:
            self._approval_handler = self._executor_pool.get_approval_handler()
        return executor

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
        delegate = self._delegate_call
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

    def stop_task_executors(self):
        """Interrompe todos os executores de tasks em segundo plano."""
        return self._executor_pool.stop_task_executors()

    # ── Overview / estado compartilhado (delegates to TaskProtocolService) ─

    def build_task_overview(self) -> dict:
        """Compila resumo das tasks abertas para o agente."""
        return self._protocol.build_task_overview()

    def refresh_task_shared_state(self) -> None:
        """Atualiza o shared_state com overview e TODOs atuais."""
        return self._protocol.refresh_task_shared_state()

    # ── Prompt factory delegates (delegates to TaskProtocolService) ─

    def task_context_history_window(self) -> int:
        """Retorna a janela de histórico configurada para tasks."""
        return self._protocol.task_context_history_window()

    def format_task_chat_context(self) -> str:
        """Serializa o histórico recente para uso em prompts de task."""
        return self._protocol.format_task_chat_context()

    def build_task_body(self, description: str) -> str:
        """Monta o payload completo de execução para uma task."""
        return self._protocol.build_task_body(description)

    # ── Task router delegates (delegates to TaskProtocolService) ───

    def get_task_routing_profiles(self):
        """Retorna os profiles elegíveis para roteamento de tasks."""
        return self._protocol.get_task_routing_profiles()

    def count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta tasks pendentes e em andamento de um agente."""
        return self._protocol.count_agent_open_tasks(agent_name)

    def choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Seleciona o melhor agente para uma task considerando carga."""
        return self._protocol.choose_agent_with_load_balance(task_type)

    # ── Handlers / comando (delegates to TaskProtocolService) ──────

    def handle_task_command(self, command: str) -> None:
        """Processa o comando /task: classifica, roteia e persiste a task."""
        self._protocol.handle_task_command(command)

    # ── Builders privados ──────────────────────────────────────────

    def _build_task_repository(self) -> TaskRepository:
        workspace = self._get_workspace()
        if workspace is None:
            raise ValueError("Workspace is required to access task repository")
        return TaskRepository(workspace.tasks_db, event_sink=self._get_event_sink())

    def _get_background_tool_executor(self) -> ToolExecutor | None:
        return self._executor_pool._get_background_tool_executor()

    def _create_background_dispatch_services(
        self,
        *,
        cancel_checker_override=None,
        cancel_event: threading.Event | None = None,
    ) -> AppDispatchServices | None:
        return self._executor_pool._create_background_dispatch_services(
            cancel_checker_override=cancel_checker_override,
            cancel_event=cancel_event,
        )

    def _get_background_dispatch_services(self) -> AppDispatchServices | None:
        if self._background_dispatch_services is not None:
            return self._background_dispatch_services
        self._background_dispatch_services = self._create_background_dispatch_services()
        return self._background_dispatch_services

    def _build_task_prompt_factory(self) -> TaskPromptFactory:
        return TaskPromptFactory(
            history=self._get_history(),
            user_name=self._get_user_name(),
            shared_state=self._get_shared_state(),
            prompt_builder=self._get_prompt_builder(),
        )

    def _build_task_execution_service(self, failover_policy: TaskFailoverPolicy) -> TaskExecutionService:
        return TaskExecutionService(
            dispatch_services=self._get_background_dispatch_services(),
            system_layer=self._get_system_layer(),
            repository=self._build_task_repository(),
            failover_policy=failover_policy,
            classify_task_execution_result=self._classify_task_execution_result,
            was_user_cancelled=self._background_was_user_cancelled,
            record_failure=self._get_record_failure(),
            before_agent_call=lambda agent_name: self._enable_task_tool_auto_approval(
                agent_name,
                approval_handler=getattr(self._get_background_tool_executor(), "approval_handler", None),
            ),
            after_agent_call=lambda agent_name: self._disable_task_tool_auto_approval(
                agent_name,
                approval_handler=getattr(self._get_background_tool_executor(), "approval_handler", None),
            ),
        )

    def _enable_task_tool_auto_approval(self, agent_name: str, approval_handler=None) -> None:
        return self._executor_pool._enable_task_tool_auto_approval(
            agent_name,
            approval_handler=approval_handler,
        )

    def _disable_task_tool_auto_approval(self, agent_name: str, approval_handler=None) -> None:
        return self._executor_pool._disable_task_tool_auto_approval(
            agent_name,
            approval_handler=approval_handler,
        )

    def _task_approval_handlers(self, approval_handler=None) -> list[Any]:
        return self._executor_pool._task_approval_handlers(approval_handler)

    def _build_task_review_service(self, failover_policy: TaskFailoverPolicy) -> TaskReviewService:
        return TaskReviewService(
            dispatch_services=self._get_background_dispatch_services(),
            system_layer=self._get_system_layer(),
            repository=self._build_task_repository(),
            failover_policy=failover_policy,
            classify_task_review_result=self._classify_task_review_result,
            was_user_cancelled=self._background_was_user_cancelled,
            event_sink=self._get_event_sink(),
        )

    def _build_task_router(self) -> TaskRouter:
        return TaskRouter(
            active_agents=self._agent_pool_agents(),
            get_agent_profile=self._get_agent_profile,
            get_available_profiles=self._get_available_profiles,
            repository=self._build_task_repository(),
        )

    def _build_task_failover_policy(self) -> TaskFailoverPolicy:
        return self._executor_pool._build_task_failover_policy()

    # ── Execution mode accessor (used by background dispatch proxy) ─

    def _get_execution_mode_value(self):
        getter = self._get_execution_mode
        return getter() if callable(getter) else None


def delegate_for_parallel(app, agent, delegation, protocol_mode, staging_root: Path, index: int):
    """Executa chamada paralela do agente isolando staging por thread."""
    return delegate_for_parallel_with_client(
        app.delegate,
        app.parse_response,
        agent,
        delegation,
        protocol_mode,
        staging_root,
        index,
    )
