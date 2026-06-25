"""Componentes do domínio `quimera.tasks`.

``AppTaskServices`` é o adaptador fino entre o ``/task`` (e demais operações
de task do ``QuimeraApp``) e o domínio de tasks composto pelos serviços:

- ``TaskRepository``       – persistência (CRUD)
- ``TaskRouter``            – roteamento e balanceamento
- ``TaskPromptFactory``     – montagem de prompt
- ``TaskExecutionService``  – execução com agente
- ``TaskReviewService``     – revisão por outro agente
- ``TaskFailoverPolicy``    – decisões de falha/requeue

A classe retém apenas bootstrap (construção dos serviços com dependências
injetadas explicitamente) e delegação rasa.
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
from ..domain.session_state import SessionState
from ..runtime.tools.todo import TodoRegistry
from .classifiers import classify_task_execution_result, classify_task_review_result, parse_task_command
from .execution import TaskExecutionService
from .failover import TaskFailoverPolicy
from .prompt import TaskPromptFactory
from .repository import TaskRepository
from .review import TaskReviewService
from .router import TaskRouter
from .utils import build_completed_task_results


_BACKGROUND_AGENT_TIMEOUT_SECONDS = 120


class _BackgroundDispatchAppProxy:
    """Adapter mínimo para reusar ``AppDispatchServices`` em tasks de background."""

    def __init__(
        self,
        *,
        task_services: "AppTaskServices",
        get_session_metrics: Callable[[], Any],
        get_round_index: Callable[[], int],
        get_debug_prompt_metrics: Callable[[], bool],
        get_redisplay_prompt: Callable[[], Callable[[], None] | None],
        get_output_lock: Callable[[], Any],
        get_counter_lock: Callable[[], Any],
        get_shared_state_lock: Callable[[], Any],
        get_session_services: Callable[[], Any],
        max_retries: int = 2,
        retry_backoff_seconds: int = 1,
        get_rate_limit_backoff_seconds: Callable[[], int],
    ) -> None:
        self.task_services = task_services
        self._get_session_metrics = get_session_metrics
        self._get_round_index = get_round_index
        self._get_debug_prompt_metrics = get_debug_prompt_metrics
        self._get_redisplay_prompt = get_redisplay_prompt
        self._get_output_lock = get_output_lock
        self._get_counter_lock = get_counter_lock
        self._get_shared_state_lock = get_shared_state_lock
        self._get_session_services = get_session_services
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._get_rate_limit_backoff_seconds = get_rate_limit_backoff_seconds

    @property
    def prompt_builder(self):
        return self.task_services._get_prompt_builder()

    @property
    def renderer(self):
        return self.task_services._get_renderer()

    @property
    def agent_run_sink(self):
        return self.task_services._get_agent_run_sink()

    def get_agent_profile(self, agent_name: str):
        return self.task_services._get_agent_profile(agent_name)

    @property
    def history(self):
        return self.task_services._get_history()

    @property
    def shared_state(self):
        return self.task_services._get_shared_state()

    @property
    def execution_mode(self):
        return self.task_services._get_execution_mode()

    @property
    def session_state(self):
        return self.task_services._get_session_state()

    @property
    def round_index(self):
        return self._get_round_index()

    @property
    def debug_prompt_metrics(self):
        return self._get_debug_prompt_metrics()

    def _redisplay_user_prompt_if_needed(self, **kw):
        callback = self._get_redisplay_prompt()
        if callable(callback):
            callback(**kw)

    @property
    def _output_lock(self):
        return self._get_output_lock()

    @property
    def _counter_lock(self):
        return self._get_counter_lock()

    @property
    def _shared_state_lock(self):
        return self._get_shared_state_lock()

    @property
    def session_metrics(self):
        return self._get_session_metrics()

    @property
    def session_services(self):
        return self._get_session_services()

    @property
    def MAX_RETRIES(self):
        return self._max_retries

    @property
    def RETRY_BACKOFF_SECONDS(self):
        return self._retry_backoff_seconds

    @property
    def RATE_LIMIT_BACKOFF_SECONDS(self):
        return self._get_rate_limit_backoff_seconds()

    @property
    def record_failure(self):
        return self.task_services._get_record_failure()

    def print_response(self, agent, response):
        dispatch_services = self.task_services._get_dispatch_services()
        if dispatch_services is not None:
            return dispatch_services.print_response(agent, response)
        renderer = self.renderer
        if renderer is not None and response is not None and hasattr(renderer, "show_message"):
            renderer.show_message(agent, response)
        return None


class AppTaskServices:
    """Adaptador entre o ``QuimeraApp`` e o domínio de tasks."""

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
        session_state: SessionState | None = None,
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
        delegate: Callable[..., Any] | None = None,
        parse_response: Callable[[Any], tuple[Any, Any, Any, Any, Any, Any]],
        classify_task_execution_result: Callable[[str | None], tuple[bool, str]] = classify_task_execution_result,
        classify_task_review_result: Callable[[str | None], tuple[bool, str, str]] = classify_task_review_result,
    ):
        """Inicializa serviços de task com dependências explícitas.

        Parâmetros:
        - ``task_executor_factory``: fábrica para criar executores de task.
        - ``get_current_job_id``: retorna o job atual no momento do uso.
        - ``get_agent_pool_agents``: retorna a lista atual de agentes ativos.
        - ``get_task_executors``: retorna a coleção atual de executores.
        - ``set_task_executors``: substitui a coleção atual de executores.
        - ``get_renderer``: retorna o renderer atual.
        - ``get_input_services``: retorna os serviços de input atuais.
        - ``get_input_gate``: retorna o gate de input atual.
        - ``get_event_sink``: retorna o event sink atual.
        - ``get_agent_run_sink``: retorna o sink atual de eventos de execução de agente.
        - ``get_agent_client``: retorna o agent client atual.
        - ``get_workspace``: retorna o workspace atual.
        - ``get_dispatch_tool_executor``: retorna o tool executor primário do dispatch.
        - ``get_dispatch_services``: retorna os serviços primários de dispatch.
        - ``get_auto_approve_mutations``: informa se mutações são auto-aprovadas.
        - ``get_approval_handler``: retorna o approval handler atual.
        - ``set_approval_handler``: registra o approval handler primário.
        - ``get_agent_profile``: resolve o profile de um agente.
        - ``get_available_profiles``: retorna profiles disponíveis para roteamento.
        - ``get_session_state``: retorna o estado atual da sessão.
        - ``get_history``: retorna o histórico atual da sessão.
        - ``get_shared_state``: retorna o shared state atual.
        - ``get_system_layer``: retorna a system layer atual.
        - ``get_task_classifier``: retorna o classificador configurado de tasks.
        - ``get_user_name``: retorna o nome atual do usuário.
        - ``get_prompt_builder``: retorna o prompt builder atual.
        - ``get_visibility``: retorna a visibilidade atual do app.
        - ``get_show_error_message``: retorna o callback atual de erro.
        - ``get_show_muted_message``: retorna o callback atual de mensagens silenciosas.
        - ``get_execution_mode``: retorna o modo de execução atual.
        - ``get_record_tool_event``: retorna o callback atual de telemetry de tools.
        - ``get_record_failure``: retorna o callback atual de falha de agente.
        - ``get_session_metrics``: retorna o coletor atual de métricas de sessão.
        - ``get_round_index``: retorna o round index atual.
        - ``get_debug_prompt_metrics``: informa se métricas de prompt estão ativas.
        - ``get_redisplay_prompt``: retorna o callback atual para redesenhar o prompt.
        - ``get_output_lock``: retorna o lock atual de output.
        - ``get_counter_lock``: retorna o lock atual de contadores.
        - ``get_shared_state_lock``: retorna o lock atual do shared state.
        - ``get_session_services``: retorna os serviços atuais de sessão.
        - ``max_retries``: limite de retries.
        - ``retry_backoff_seconds``: backoff padrão de retry.
        - ``get_rate_limit_backoff_seconds``: retorna o backoff para rate limit.
            - ``delegate``: executa chamada de agente para delegações paralelas.
        - ``parse_response``: parseia a resposta crua de agente.
        - ``classify_task_execution_result``: classifica sucesso/falha da execução da task.
        - ``classify_task_review_result``: classifica aceite/rejeição/retry do review.
        """
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
        # SessionState (preferred) and compat lambda fallbacks
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
        self._delegate = delegate
        self._parse_response = parse_response
        self._classify_task_execution_result = classify_task_execution_result
        self._classify_task_review_result = classify_task_review_result
        self._background_dispatch_services: AppDispatchServices | None = None
        self._background_tool_executor: ToolExecutor | None = None

    # ── SessionState accessors ─────────────────────────────────────────

    def _get_session_state(self) -> dict | None:
        if self._session_state_obj is not None:
            return self._session_state_obj.session_meta
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
        if self._session_state_obj is not None:
            return self._session_state_obj.history
        return self._get_history_fn() if self._get_history_fn else []

    def _get_shared_state(self) -> dict | None:
        if self._session_state_obj is not None:
            return self._session_state_obj.shared_state
        return self._get_shared_state_fn() if self._get_shared_state_fn else None

    def _get_round_index(self) -> int:
        if self._session_state_obj is not None:
            return self._session_state_obj.round_index
        return self._get_round_index_fn() if self._get_round_index_fn else 0

    def _get_shared_state_lock(self) -> Any:
        if self._session_state_obj is not None:
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

    def _get_show_error_message(self):
        if self._show_error_message is not None:
            return self._show_error_message
        return self._show_error_message_getter() if self._show_error_message_getter else None

    def _get_show_muted_message(self):
        if self._show_muted_message is not None:
            return self._show_muted_message
        return self._show_muted_message_getter() if self._show_muted_message_getter else None

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

    def bind_dispatch_services(self, dispatch_services: AppDispatchServices | None) -> None:
        """Associa explicitamente os serviços primários de dispatch após o bootstrap."""
        self._dispatch_services = dispatch_services

    def bind_dispatch_tool_executor(self, tool_executor: ToolExecutor | None) -> None:
        """Associa explicitamente o ToolExecutor primário após sua criação."""
        self._dispatch_tool_executor = tool_executor

    def bind_session_services(self, session_services: Any) -> None:
        """Associa explicitamente os serviços de sessão após o bootstrap."""
        self._session_services = session_services

    def bind_primary_approval_handler(self, approval_handler: Any) -> None:
        """Associa explicitamente o approval handler primário após o bootstrap."""
        self._approval_handler = approval_handler

    # ── Setup / bootstrap ──────────────────────────────────────────────

    def setup_task_executors(self):
        """Inicializa executores assíncronos para tasks humanas."""
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
            executor.start()
            executors.append(executor)
        self._replace_task_executors(executors)

    def build_tool_executor(
        self,
        require_approval_for_mutations: bool = True,
        *,
        register_as_primary: bool = True,
        allow_ask_user: bool = True,
    ) -> ToolExecutor:
        """Cria o executor de ferramentas do app com a configuração padrão."""
        renderer = self._get_renderer()
        input_services = self._get_input_services()
        input_gate = self._get_input_gate()
        workspace = self._get_workspace()
        rt_config = ToolRuntimeConfig(
            workspace_root=workspace.cwd,
            db_path=workspace.tasks_db,
            memory_file=getattr(workspace, "memory_file", None),
            require_approval_for_mutations=require_approval_for_mutations,
            allow_ask_user=allow_ask_user,
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

    def delegate_for_parallel(
        self,
        agent,
        delegation,
        protocol_mode,
        staging_root: Path,
        index: int,
        cancel_event: threading.Event | None = None,
    ):
        """Executa chamada paralela isolando staging e cliente por worker."""
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
        """Interrompe executores de tasks em segundo plano."""
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

    # ── Overview / estado compartilhado ─────────────────────────────────

    def build_task_overview(self) -> dict:
        """Resumo do estado atual das tasks abertas (delega ao repositório)."""
        repo = self._build_task_repository()
        current_job_id = self._current_job_id()
        try:
            job = repo.get_job(current_job_id)
            open_tasks = []
            for status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                open_tasks.extend(
                    repo.list_tasks({"job_id": current_job_id, "status": status})
                )
            open_tasks.sort(key=lambda task: task.id)
            counts = {
                "pending": sum(1 for task in open_tasks if task.status == TaskStatus.PENDING),
                "in_progress": sum(1 for task in open_tasks if task.status == TaskStatus.IN_PROGRESS),
            }
            preview = [
                {
                    "id": task.id,
                    "status": task.status,
                    "priority": task.priority,
                    "task_type": task.task_type,
                    "assigned_to": task.assigned_to,
                    "description": task.description,
                }
                for task in open_tasks[:6]
            ]
            if counts["pending"] > 0:
                recommended = "Há tasks pendentes criadas pelo humano aguardando execução."
            elif counts["in_progress"] > 0:
                recommended = "Há trabalho em andamento; acompanhe antes de abrir tarefas paralelas."
            else:
                recommended = "Sem tarefas abertas; novas tasks só podem ser criadas pelo humano com /task."
            return {
                "job_id": current_job_id,
                "job_description": job.description if job else None,
                "open_task_counts": counts,
                "open_tasks_preview": preview,
                "recommended_action": recommended,
            }
        except Exception as exc:
            return {"job_id": current_job_id, "error": str(exc)}

    def refresh_task_shared_state(self) -> None:
        """Sincroniza estado compartilhado de tasks no app (delega ao repositório)."""
        shared_state = self._get_shared_state()
        current_job_id = self._current_job_id()
        workspace = self._get_workspace()
        if not isinstance(shared_state, dict) or current_job_id is None or workspace is None:
            return
        shared_state["task_overview"] = self.build_task_overview()
        try:
            shared_state["agent_todos"] = TodoRegistry.get_active_as_dicts(current_job_id)
        except Exception as exc:
            logger.debug("agent_todos falhou: %s", exc)
            shared_state["agent_todos"] = []
        repo = self._build_task_repository()
        completed_tasks = repo.list_tasks(
            {"job_id": current_job_id, "status": "completed"}
        )
        if completed_tasks:
            completed_summary = build_completed_task_results(completed_tasks)
            if completed_summary:
                shared_state["completed_task_results"] = completed_summary
            else:
                shared_state.pop("completed_task_results", None)
        else:
            shared_state.pop("completed_task_results", None)

    # ── Prompt factory delegates ───────────────────────────────────────

    def task_context_history_window(self) -> int:
        """Janela de histórico usada no contexto de tasks."""
        return self._build_task_prompt_factory().task_context_history_window()

    def format_task_chat_context(self) -> str:
        """Serializa histórico recente para uso em prompts de task."""
        return self._build_task_prompt_factory().format_task_chat_context()

    def build_task_body(self, description: str) -> str:
        """Monta o payload completo de execução de uma task."""
        return self._build_task_prompt_factory().build_task_body(description)

    # ── Task router delegates ──────────────────────────────────────────

    def get_task_routing_profiles(self):
        """Retorna profiles elegíveis para roteamento de tasks."""
        return self._build_task_router().get_task_routing_profiles()

    def count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta tasks abertas associadas ao agente."""
        return self._build_task_router().count_agent_open_tasks(agent_name)

    def choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Seleciona melhor agente considerando carga."""
        return self._build_task_router().choose_agent_with_load_balance(task_type)

    # ── Handlers / comando ─────────────────────────────────────────────

    def handle_task_command(self, command: str) -> None:
        """Processa o comando ``/task <descrição>``."""
        renderer = self._get_renderer()
        description = parse_task_command(command)
        if not description:
            if renderer is not None:
                renderer.show_warning("Uso: /task <descrição>")
            return

        task_classifier = self._get_task_classifier()
        if task_classifier is not None and not hasattr(task_classifier, "classify"):
            logger.debug(
                "task_classifier inválido (%s): fallback para classificador padrão",
                type(task_classifier).__name__,
            )
            task_classifier = None
        classification = classify_task(description, classifier=task_classifier)
        task_type = classification.task_type
        selected_agent = self.choose_agent_with_load_balance(task_type)

        repo = self._build_task_repository()
        user_name = self._get_user_name()
        task_id = repo.create_task(
            self._current_job_id(),
            description,
            task_type=task_type,
            assigned_to=selected_agent,
            origin="human_command",
            status="pending",
            created_by=user_name,
            requested_by=user_name,
            body=self.build_task_body(description),
            source_context=command,
        )
        for executor in self._task_executors():
            if hasattr(executor, "wake"):
                executor.wake()
        self.refresh_task_shared_state()
        lines = [f"task criada com id {task_id}"]
        if selected_agent:
            lines.append(f"atribuída para {selected_agent}")
        lines.append(f"tipo inferido: {task_type}")
        system_layer = self._get_system_layer()
        if system_layer is not None:
            system_layer.show_system_message(" | ".join(lines))

    # ── Builders privados ──────────────────────────────────────────────

    def _build_task_repository(self) -> TaskRepository:
        workspace = self._get_workspace()
        if workspace is None:
            raise ValueError("Workspace is required to access task repository")
        return TaskRepository(workspace.tasks_db, event_sink=self._get_event_sink())

    def _was_user_cancelled(self) -> bool:
        agent_client = self._get_agent_client()
        return bool(agent_client and agent_client._user_cancelled)

    def _background_was_user_cancelled(self) -> bool:
        return False

    def _get_background_tool_executor(self) -> ToolExecutor | None:
        if self._background_tool_executor is None:
            if self._get_workspace() is None:
                return self._get_dispatch_tool_executor()
            self._background_tool_executor = self.build_tool_executor(
                require_approval_for_mutations=not self._get_auto_approve_mutations(),
                register_as_primary=False,
                allow_ask_user=False,
            )
        return self._background_tool_executor

    def _create_background_dispatch_services(
        self,
        *,
        cancel_checker_override=None,
        cancel_event: threading.Event | None = None,
    ) -> AppDispatchServices | None:
        renderer = self._get_renderer()
        workspace = self._get_workspace()
        if renderer is None or workspace is None:
            return self._get_dispatch_services()

        chat_agent_client = self._get_agent_client()
        if chat_agent_client is None:
            return self._get_dispatch_services()
        background_timeout = getattr(chat_agent_client, "idle_timeout", None)
        if background_timeout is None or not isinstance(background_timeout, (int, float)) or background_timeout <= 0:
            background_timeout = _BACKGROUND_AGENT_TIMEOUT_SECONDS
        _muted = self._get_show_muted_message()
        session_state = self._get_session_state()
        workspace_tmp = getattr(workspace, "tmp", None)
        workspace_tmp_root = getattr(workspace_tmp, "root", None)
        background_agent_client = AgentClient(
            renderer,
            idle_timeout=background_timeout,
            visibility=self._get_visibility(),
            working_dir=str(workspace.cwd),
            error_reporter=_muted,
            muted_reporter=_muted,
            session_id=session_state.get("session_id") if isinstance(session_state, dict) else None,
            workspace_tmp_root=workspace_tmp_root,
        )
        background_agent_client.execution_mode = self._get_execution_mode()
        background_agent_client.tool_event_callback = self._get_record_tool_event()
        background_agent_client.tool_executor = self._get_background_tool_executor()
        if cancel_event is not None:
            background_agent_client._cancel_event = cancel_event
        proxy = _BackgroundDispatchAppProxy(
            task_services=self,
            get_session_metrics=self._get_session_metrics,
            get_round_index=self._get_round_index,
            get_debug_prompt_metrics=self._get_debug_prompt_metrics,
            get_redisplay_prompt=self._get_redisplay_prompt,
            get_output_lock=self._get_output_lock,
            get_counter_lock=self._get_counter_lock,
            get_shared_state_lock=self._get_shared_state_lock,  # method reference
            get_session_services=self._get_session_services,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
            get_rate_limit_backoff_seconds=self._get_rate_limit_backoff_seconds,
        )
        return AppDispatchServices.from_app(
            proxy,
            agent_client_override=background_agent_client,
            tool_executor_override=background_agent_client.tool_executor,
            cancel_checker_override=cancel_checker_override or self._background_was_user_cancelled,
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
        for handler in self._task_approval_handlers(approval_handler):
            handler.set_thread_approve_all(
                True,
                scope_key=f"task:{agent_name}:{id(self)}",
                silent=True,
            )

    def _disable_task_tool_auto_approval(self, agent_name: str, approval_handler=None) -> None:
        for handler in self._task_approval_handlers(approval_handler):
            handler.set_thread_approve_all(False, scope_key=f"task:{agent_name}:{id(self)}")

    def _task_approval_handlers(self, approval_handler=None) -> list[Any]:
        handlers: list[Any] = []
        for handler in (approval_handler, self._get_approval_handler()):
            if handler is None or not hasattr(handler, "set_thread_approve_all"):
                continue
            if any(existing is handler for existing in handlers):
                continue
            handlers.append(handler)
        return handlers

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
        return TaskFailoverPolicy(
            active_agents=self._agent_pool_agents,
            get_agent_profile=self._get_agent_profile,
            repository=self._build_task_repository(),
        )


def delegate_for_parallel_with_client(
    delegate: Callable[..., Any],
    parse_response: Callable[[Any], tuple[Any, ...]],
    agent,
    delegation,
    protocol_mode,
    staging_root: Path,
    index: int,
):
    """Executa chamada paralela do agente isolando staging por thread."""
    from ..runtime.tools.files import set_staging_root

    set_staging_root(staging_root / str(index))
    try:
        raw = delegate(agent, delegation=delegation, primary=False, protocol_mode=protocol_mode, silent=True, show_output=False)
        response, _, _, extend, needs_input, _ = parse_response(raw)
        return agent, response, extend, needs_input
    finally:
        set_staging_root(None)


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
