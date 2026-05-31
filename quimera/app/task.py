"""Componentes de ``quimera.app.task``.

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
from ..runtime import ConsoleApprovalHandler, PreApprovalHandler, ToolRuntimeConfig, create_executor
from ..runtime.executor import ToolExecutor
from ..runtime.task_planning import classify_task
from .config import logger
from .dispatch import AppDispatchServices
from ..domain.session_state import SessionState
from ..runtime.tools.todo import TodoRegistry
from .task_classifiers import classify_task_execution_result, classify_task_review_result, parse_task_command
from .task_execution_service import TaskExecutionService
from .task_failover_policy import TaskFailoverPolicy
from .task_prompt_factory import TaskPromptFactory
from .task_repository import TaskRepository
from .task_review_service import TaskReviewService
from .task_router import TaskRouter
from .task_utils import build_completed_task_results


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

    def get_agent_plugin(self, agent_name: str):
        return self.task_services._get_agent_plugin(agent_name)

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
        get_current_job_id: Callable[[], Any],
        get_agent_pool_agents: Callable[[], list[Any]],
        get_task_executors: Callable[[], list[Any]],
        set_task_executors: Callable[[list[Any]], None],
        get_renderer: Callable[[], Any],
        get_input_services: Callable[[], Any],
        get_input_gate: Callable[[], Any],
        get_tasks_db_path: Callable[[], str | None],
        get_event_sink: Callable[[], Any],
        get_agent_client: Callable[[], Any],
        get_workspace: Callable[[], Any],
        get_dispatch_tool_executor: Callable[[], ToolExecutor | None],
        get_dispatch_services: Callable[[], AppDispatchServices | None],
        get_auto_approve_mutations: Callable[[], bool],
        get_approval_handler: Callable[[], Any],
        set_approval_handler: Callable[[Any], None],
        get_agent_plugin: Callable[[str], Any],
        get_available_plugins: Callable[[], list[Any]],
        session_state: SessionState | None = None,
        get_session_state: Callable[[], dict[str, Any] | None] | None = None,
        get_history: Callable[[], Any] | None = None,
        get_shared_state: Callable[[], dict[str, Any] | None] | None = None,
        get_round_index: Callable[[], int] | None = None,
        get_shared_state_lock: Callable[[], Any] | None = None,
        get_system_layer: Callable[[], Any] = None,
        get_task_classifier: Callable[[], Any] = None,
        get_user_name: Callable[[], str] = None,
        get_prompt_builder: Callable[[], Any] = None,
        get_visibility: Callable[[], Any] = None,
        get_show_error_message: Callable[[], Callable[[str], None] | None] = None,
        get_show_muted_message: Callable[[], Callable[[str], None] | None] = None,
        get_execution_mode: Callable[[], Any] = None,
        get_record_tool_event: Callable[[], Callable[..., None] | None] = None,
        get_record_failure: Callable[[], Callable[[str], None] | None] = None,
        get_session_metrics: Callable[[], Any] = None,
        get_debug_prompt_metrics: Callable[[], bool] = None,
        get_redisplay_prompt: Callable[[], Callable[[], None] | None] = None,
        get_output_lock: Callable[[], Any] = None,
        get_counter_lock: Callable[[], Any] = None,
        get_session_services: Callable[[], Any] = None,
        max_retries: int = 2,
        retry_backoff_seconds: int = 1,
        get_rate_limit_backoff_seconds: Callable[[], int],
        call_agent: Callable[..., Any],
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
        - ``get_tasks_db_path``: retorna o caminho atual do banco de tasks.
        - ``get_event_sink``: retorna o event sink atual.
        - ``get_agent_client``: retorna o agent client atual.
        - ``get_workspace``: retorna o workspace atual.
        - ``get_dispatch_tool_executor``: retorna o tool executor primário do dispatch.
        - ``get_dispatch_services``: retorna os serviços primários de dispatch.
        - ``get_auto_approve_mutations``: informa se mutações são auto-aprovadas.
        - ``get_approval_handler``: retorna o approval handler atual.
        - ``set_approval_handler``: registra o approval handler primário.
        - ``get_agent_plugin``: resolve o plugin de um agente.
        - ``get_available_plugins``: retorna plugins disponíveis para roteamento.
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
        - ``call_agent``: executa chamada de agente para handoffs paralelos.
        - ``parse_response``: parseia a resposta crua de agente.
        - ``classify_task_execution_result``: classifica sucesso/falha da execução da task.
        - ``classify_task_review_result``: classifica aceite/rejeição/retry do review.
        """
        self._task_executor_factory = task_executor_factory
        self._get_current_job_id = get_current_job_id
        self._get_agent_pool_agents = get_agent_pool_agents
        self._get_task_executors = get_task_executors
        self._set_task_executors = set_task_executors
        self._get_renderer = get_renderer
        self._get_input_services = get_input_services
        self._get_input_gate = get_input_gate
        self._get_tasks_db_path = get_tasks_db_path
        self._get_event_sink = get_event_sink
        self._get_agent_client = get_agent_client
        self._get_workspace = get_workspace
        self._get_dispatch_tool_executor = get_dispatch_tool_executor
        self._get_dispatch_services = get_dispatch_services
        self._get_auto_approve_mutations = get_auto_approve_mutations
        self._get_approval_handler = get_approval_handler
        self._set_approval_handler = set_approval_handler
        self._get_agent_plugin = get_agent_plugin
        self._get_available_plugins = get_available_plugins
        # SessionState (preferred) and compat lambda fallbacks
        self._session_state_obj = session_state
        self._get_session_state_fn = get_session_state
        self._get_history_fn = get_history
        self._get_shared_state_fn = get_shared_state
        self._get_round_index_fn = get_round_index
        self._get_shared_state_lock_fn = get_shared_state_lock
        self._get_system_layer = get_system_layer
        self._get_task_classifier = get_task_classifier
        self._get_user_name = get_user_name
        self._get_prompt_builder = get_prompt_builder
        self._get_visibility = get_visibility
        self._get_show_error_message = get_show_error_message
        self._get_show_muted_message = get_show_muted_message
        self._get_execution_mode = get_execution_mode
        self._get_record_tool_event = get_record_tool_event
        self._get_record_failure = get_record_failure
        self._get_session_metrics = get_session_metrics
        self._get_debug_prompt_metrics = get_debug_prompt_metrics
        self._get_redisplay_prompt = get_redisplay_prompt
        self._get_output_lock = get_output_lock
        self._get_counter_lock = get_counter_lock
        self._get_session_services = get_session_services
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._get_rate_limit_backoff_seconds = get_rate_limit_backoff_seconds
        self._call_agent = call_agent
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

    # ── Setup / bootstrap ──────────────────────────────────────────────

    def setup_task_executors(self):
        """Inicializa executores assíncronos para tasks humanas."""
        failover_policy = self._build_task_failover_policy()
        task_execution_service = self._build_task_execution_service(failover_policy)
        task_review_service = self._build_task_review_service(failover_policy)

        repository = self._build_task_repository()
        job_id = self._get_current_job_id()
        executors = []
        for agent in list(self._get_agent_pool_agents() or []):
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
        self._set_task_executors(executors)

    def build_tool_executor(
        self,
        require_approval_for_mutations: bool = True,
        *,
        register_as_primary: bool = True,
    ) -> ToolExecutor:
        """Cria o executor de ferramentas do app com a configuração padrão."""
        renderer = self._get_renderer()
        input_services = self._get_input_services()
        input_gate = self._get_input_gate()
        base_handler = ConsoleApprovalHandler(
            renderer=renderer,
            suspend_fn=input_services.suspend_nonblocking if input_services else None,
            resume_fn=input_services.resume_nonblocking if input_services else None,
            input_gate=input_gate,
        )
        approval_handler = PreApprovalHandler(base_handler)
        base_handler.set_approve_all_callback(approval_handler.set_approve_all)
        if register_as_primary:
            self._set_approval_handler(approval_handler)
        workspace = self._get_workspace()
        tasks_db_path = self._get_tasks_db_path()
        return ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=workspace.cwd,
                db_path=Path(tasks_db_path) if tasks_db_path else None,
                require_approval_for_mutations=require_approval_for_mutations,
            ),
            approval_handler=approval_handler,
        )

    def call_agent_for_parallel(
        self,
        agent,
        handoff,
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
        call_agent = self._call_agent
        if background_dispatch is not None:
            call_agent = background_dispatch.call_agent
        try:
            return call_agent_for_parallel_with_client(
                call_agent,
                self._parse_response,
                agent,
                handoff,
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
        for executor in self._get_task_executors() or []:
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
        job_id = self._get_current_job_id()
        if job_id is not None:
            TodoRegistry.cleanup(job_id)

    # ── Overview / estado compartilhado ─────────────────────────────────

    def build_task_overview(self) -> dict:
        """Resumo do estado atual das tasks abertas (delega ao repositório)."""
        repo = self._build_task_repository()
        current_job_id = self._get_current_job_id()
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
        current_job_id = self._get_current_job_id()
        if not isinstance(shared_state, dict) or current_job_id is None or not self._get_tasks_db_path():
            return
        shared_state["task_overview"] = self.build_task_overview()
        try:
            shared_state["agent_todos"] = TodoRegistry.get_active_as_dicts(current_job_id)
        except Exception as exc:
            logger.warning("agent_todos falhou: %s", exc)
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

    def get_task_routing_plugins(self):
        """Retorna plugins elegíveis para roteamento de tasks."""
        return self._build_task_router().get_task_routing_plugins()

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
            logger.warning(
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
            self._get_current_job_id(),
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
        for executor in self._get_task_executors() or []:
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
        db_path = self._get_tasks_db_path()
        if not db_path:
            raise ValueError("tasks_db_path is required to access task repository")
        return TaskRepository(db_path, event_sink=self._get_event_sink())

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
        background_timeout = getattr(chat_agent_client, "timeout", None)
        if background_timeout is None or not isinstance(background_timeout, (int, float)) or background_timeout <= 0:
            background_timeout = _BACKGROUND_AGENT_TIMEOUT_SECONDS
        _muted = self._get_show_muted_message()
        session_state = self._get_session_state()
        workspace_tmp = getattr(workspace, "tmp", None)
        workspace_tmp_root = getattr(workspace_tmp, "root", None)
        background_agent_client = AgentClient(
            renderer,
            timeout=background_timeout,
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
        plugin = self._get_agent_plugin(agent_name)
        effective_driver = getattr(plugin, "effective_driver", None)
        supports_tools = bool(getattr(plugin, "supports_tools", False))
        driver_name = effective_driver() if callable(effective_driver) else None
        if plugin is None or driver_name != "openai_compat" or not supports_tools:
            return
        if approval_handler is None:
            approval_handler = self._get_approval_handler()
        if approval_handler is not None and hasattr(approval_handler, "set_thread_approve_all"):
            approval_handler.set_thread_approve_all(
                True,
                scope_key=f"task:{agent_name}:{id(self)}",
                silent=True,
            )

    def _disable_task_tool_auto_approval(self, agent_name: str, approval_handler=None) -> None:
        plugin = self._get_agent_plugin(agent_name)
        effective_driver = getattr(plugin, "effective_driver", None)
        supports_tools = bool(getattr(plugin, "supports_tools", False))
        driver_name = effective_driver() if callable(effective_driver) else None
        if plugin is None or driver_name != "openai_compat" or not supports_tools:
            return
        if approval_handler is None:
            approval_handler = self._get_approval_handler()
        if approval_handler is not None and hasattr(approval_handler, "set_thread_approve_all"):
            approval_handler.set_thread_approve_all(False, scope_key=f"task:{agent_name}:{id(self)}")

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
            active_agents=list(self._get_agent_pool_agents() or []),
            get_agent_plugin=self._get_agent_plugin,
            get_available_plugins=self._get_available_plugins,
            repository=self._build_task_repository(),
        )

    def _build_task_failover_policy(self) -> TaskFailoverPolicy:
        return TaskFailoverPolicy(
            active_agents=self._get_agent_pool_agents,
            get_agent_plugin=self._get_agent_plugin,
            repository=self._build_task_repository(),
        )


def call_agent_for_parallel_with_client(
    call_agent: Callable[..., Any],
    parse_response: Callable[[Any], tuple[Any, ...]],
    agent,
    handoff,
    protocol_mode,
    staging_root: Path,
    index: int,
):
    """Executa chamada paralela do agente isolando staging por thread."""
    from ..runtime.tools.files import set_staging_root

    set_staging_root(staging_root / str(index))
    try:
        raw = call_agent(agent, handoff=handoff, primary=False, protocol_mode=protocol_mode, silent=True, show_output=False)
        response, _, _, extend, needs_input, _ = parse_response(raw)
        return agent, response, extend, needs_input
    finally:
        set_staging_root(None)


def call_agent_for_parallel(app, agent, handoff, protocol_mode, staging_root: Path, index: int):
    """Executa chamada paralela do agente isolando staging por thread."""
    return call_agent_for_parallel_with_client(
        app.call_agent,
        app.parse_response,
        agent,
        handoff,
        protocol_mode,
        staging_root,
        index,
    )
