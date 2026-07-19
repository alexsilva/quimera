"""Serviço de protocolo de tasks: comando /task, roteamento, prompts e overview.

Extraído de ``AppTaskServices`` na Fase 4 da refatoração arquitetural
(PLAN_APP_CORE_REFACTOR.md). Agrupa responsabilidades de parsing,
classificação, roteamento, montagem de prompt e overview de tasks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from ..constants import TaskStatus
from ..app.config import logger
from .classifiers import parse_task_command
from .planning import classify_task
from .prompt import TaskPromptFactory
from .repository import TaskRepository
from .router import TaskRouter
from .utils import build_completed_task_results


@dataclass(frozen=True)
class TaskCreationResult:
    """Recibo estruturado de uma task criada pelo protocolo."""

    task_id: int
    job_id: int
    assigned_to: str | None
    task_type: str
    status: str = TaskStatus.PENDING.value

    def as_dict(self) -> dict[str, Any]:
        """Serializa o recibo para adapters de UI e ferramentas."""
        return asdict(self)


class TaskProtocolService:
    """Comando /task, roteamento, prompts e overview de tasks.

    Serviço leve que encapsula toda a lógica de protocolo de tasks:
    parsing de comandos, classificação, roteamento, montagem de prompts
    e manutenção do overview / shared state.
    """

    def __init__(
        self,
        *,
        workspace: Any = None,
        get_workspace: Callable[[], Any] | None = None,
        agent_pool: Any,
        get_active_agents: Callable[[], list[Any]] | None = None,
        profile_resolver: Any,
        get_agent_profile: Callable[[str], Any] | None = None,
        get_available_profiles: Callable[[], list[Any]] | None = None,
        task_classifier: Any | None = None,
        get_task_classifier: Callable[[], Any] | None = None,
        user_name: str | None = None,
        get_user_name: Callable[[], str | None] | None = None,
        system_layer: Any = None,
        get_system_layer: Callable[[], Any] | None = None,
        prompt_builder: Any = None,
        get_prompt_builder: Callable[[], Any] | None = None,
        get_current_job_id: Callable[[], Any],
        get_event_sink: Callable[[], Any],
        get_shared_state: Callable[[], dict[str, Any] | None],
        get_history: Callable[[], Any] | None = None,
        session_state: Any = None,
        wake_executors: Callable[[], None] | None = None,
        get_renderer: Callable[[], Any] | None = None,
    ) -> None:
        self._workspace = workspace
        self._get_workspace_fn = get_workspace
        self._agent_pool = agent_pool
        self._get_active_agents_fn = get_active_agents
        self._profile_resolver = profile_resolver
        self._get_agent_profile_fn = get_agent_profile
        self._get_available_profiles_fn = get_available_profiles
        self._task_classifier = task_classifier
        self._get_task_classifier_fn = get_task_classifier
        self._user_name = user_name
        self._get_user_name_fn = get_user_name
        self._system_layer = system_layer
        self._get_system_layer_fn = get_system_layer
        self._prompt_builder = prompt_builder
        self._get_prompt_builder_fn = get_prompt_builder
        self._get_current_job_id = get_current_job_id
        self._get_event_sink = get_event_sink
        self._get_shared_state_fn = get_shared_state
        self._get_history_fn = get_history
        self._session_state = session_state
        self._wake_executors = wake_executors or (lambda: None)
        self._get_renderer_fn = get_renderer

    # ── Comando /task ──────────────────────────────────────────────────

    def handle_task_command(self, command: str) -> None:
        """Processa o comando /task: classifica, roteia e persiste a task."""
        renderer = self._get_renderer_fn() if self._get_renderer_fn else None
        description = parse_task_command(command)
        if not description:
            if renderer is not None:
                renderer.show_warning("Uso: /task <descrição>")
            return

        user_name = self._get_user_name()
        result = self.create_task(
            description,
            origin="human_command",
            requested_by=user_name,
            source_context=command,
        )
        lines = [f"task criada com id {result.task_id}"]
        if result.assigned_to:
            lines.append(f"atribuída para {result.assigned_to}")
        lines.append(f"tipo inferido: {result.task_type}")
        system_layer = self._get_system_layer()
        if system_layer is not None:
            system_layer.show_system_message(" | ".join(lines))
        elif renderer is not None:
            renderer.show_system(" | ".join(lines))

    def create_task(
        self,
        description: str,
        *,
        origin: str,
        requested_by: str | None,
        source_context: str | None,
    ) -> TaskCreationResult:
        """Classifica, roteia e persiste uma task independentemente do adapter."""
        description = str(description or "").strip()
        if not description:
            raise ValueError("description is required")

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
        current_job_id = self._get_current_job_id()
        if current_job_id is None:
            raise RuntimeError("current job is unavailable")
        task_id = repo.create_task(
            current_job_id,
            description,
            task_type=task_type,
            assigned_to=selected_agent,
            origin=origin,
            status="pending",
            created_by=requested_by,
            requested_by=requested_by,
            body=self.build_task_body(description),
            source_context=source_context,
        )
        self._wake_executors()
        self.refresh_task_shared_state()
        return TaskCreationResult(
            task_id=task_id,
            job_id=current_job_id,
            assigned_to=selected_agent,
            task_type=task_type.value,
        )

    # ── Task router delegates ──────────────────────────────────────────

    def get_task_routing_profiles(self):
        """Retorna os profiles elegíveis para roteamento de tasks."""
        return self._build_task_router().get_task_routing_profiles()

    def count_agent_open_tasks(self, agent_name: str) -> int:
        """Conta tasks pendentes e em andamento de um agente."""
        return self._build_task_router().count_agent_open_tasks(agent_name)

    def choose_agent_with_load_balance(self, task_type: str) -> str | None:
        """Seleciona o melhor agente para uma task considerando carga."""
        return self._build_task_router().choose_agent_with_load_balance(task_type)

    # ── Prompt factory delegates ───────────────────────────────────────

    def task_context_history_window(self) -> int:
        """Retorna a janela de histórico configurada para tasks."""
        return self._build_task_prompt_factory().task_context_history_window()

    def format_task_chat_context(self) -> str:
        """Serializa o histórico recente para uso em prompts de task."""
        return self._build_task_prompt_factory().format_task_chat_context()

    def build_task_body(self, description: str) -> str:
        """Monta o payload completo de execução para uma task."""
        return self._build_task_prompt_factory().build_task_body(description)

    # ── Overview / estado compartilhado ─────────────────────────────────

    def build_task_overview(self) -> dict:
        """Compila resumo das tasks abertas para o agente."""
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
                recommended = "Há tasks pendentes aguardando execução."
            elif counts["in_progress"] > 0:
                recommended = "Há trabalho em andamento; acompanhe antes de abrir tarefas paralelas."
            else:
                recommended = "Sem tarefas abertas; use /task ou a tool tasks para criar uma nova."
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
        """Atualiza o shared_state com overview e TODOs atuais."""
        shared_state = self._get_shared_state_fn()
        current_job_id = self._get_current_job_id()
        workspace = self._workspace
        if workspace is None and callable(self._get_workspace_fn):
            workspace = self._get_workspace_fn()
        if not isinstance(shared_state, dict) or current_job_id is None or workspace is None:
            return
        shared_state["task_overview"] = self.build_task_overview()
        try:
            from ..runtime.tools.todo import TodoRegistry
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

    # ── Builders privados ──────────────────────────────────────────────

    def _build_task_repository(self) -> TaskRepository:
        workspace = self._workspace
        if workspace is None and callable(self._get_workspace_fn):
            workspace = self._get_workspace_fn()
        if workspace is None:
            raise ValueError("Workspace is required to access task repository")
        return TaskRepository(workspace.tasks_db, event_sink=self._get_event_sink())

    def _build_task_router(self) -> TaskRouter:
        return TaskRouter(
            active_agents=self._active_agents(),
            get_agent_profile=self._resolve_agent_profile(),
            get_available_profiles=self._resolve_available_profiles(),
            repository=self._build_task_repository(),
        )

    def _build_task_prompt_factory(self) -> TaskPromptFactory:
        history = []
        if self._session_state is not None:
            history = self._session_state.history or []
        elif callable(self._get_history_fn):
            history = self._get_history_fn() or []
        shared_state = self._get_shared_state_fn()
        return TaskPromptFactory(
            history=history,
            user_name=self._get_user_name(),
            shared_state=shared_state,
            prompt_builder=self._get_prompt_builder(),
        )

    # ── Resolved callables ─────────────────────────────────────────────

    def _active_agents(self) -> list[Any]:
        if callable(self._get_active_agents_fn):
            return list(self._get_active_agents_fn() or [])
        return list(getattr(self._agent_pool, "agents", []) or [])

    def _resolve_agent_profile(self) -> Callable[[str], Any]:
        if callable(self._get_agent_profile_fn):
            return self._get_agent_profile_fn
        resolver = self._profile_resolver
        if resolver is not None and hasattr(resolver, "get"):
            return resolver.get
        return lambda _name: None

    def _resolve_available_profiles(self) -> Callable[[], list[Any]]:
        if callable(self._get_available_profiles_fn):
            return self._get_available_profiles_fn
        resolver = self._profile_resolver
        if resolver is not None and hasattr(resolver, "profiles"):
            return lambda: resolver.profiles
        return lambda: []

    def _get_task_classifier(self):
        if self._task_classifier is not None:
            return self._task_classifier
        return self._get_task_classifier_fn() if callable(self._get_task_classifier_fn) else None

    def _get_user_name(self):
        if self._user_name is not None:
            return self._user_name
        return self._get_user_name_fn() if callable(self._get_user_name_fn) else None

    def _get_prompt_builder(self):
        if self._prompt_builder is not None:
            return self._prompt_builder
        return self._get_prompt_builder_fn() if callable(self._get_prompt_builder_fn) else None

    def _get_system_layer(self):
        if self._system_layer is not None:
            return self._system_layer
        return self._get_system_layer_fn() if callable(self._get_system_layer_fn) else None
