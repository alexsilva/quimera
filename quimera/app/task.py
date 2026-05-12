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
extraídas do ``app``) e delegação rasa.
"""

from __future__ import annotations

from pathlib import Path

from ..constants import TaskStatus
from ..runtime import ConsoleApprovalHandler, PreApprovalHandler, create_executor
from ..runtime import ToolRuntimeConfig
from ..runtime.executor import ToolExecutor
from ..runtime.task_planning import classify_task_type
from .task_classifiers import (
    classify_task_execution_result,
    classify_task_review_result,
    parse_task_command,
)
from .task_execution_service import TaskExecutionService
from .task_failover_policy import TaskFailoverPolicy
from .task_prompt_factory import TaskPromptFactory
from .task_review_service import TaskReviewService
from .task_repository import TaskRepository
from .task_router import TaskRouter
from .task_utils import (
    build_completed_task_results,
)


class AppTaskServices:
    """Adaptador entre o ``QuimeraApp`` e o domínio de tasks.

    Constrói serviços sob demanda com dependências extraídas do objeto
    ``app`` e delega operações de domínio para os serviços especializados.
    """

    def __init__(self, app):
        self.app = app

    # ── Setup / bootstrap ──────────────────────────────────────────────

    def setup_task_executors(self):
        """Inicializa executores assíncronos para tasks humanas."""
        app = self.app
        task_executor_factory = getattr(app, "task_executor_factory", create_executor)
        failover_policy = self._build_task_failover_policy()
        task_execution_service = self._build_task_execution_service(failover_policy)
        task_review_service = self._build_task_review_service(failover_policy)

        job_id = getattr(app, "current_job_id", None)
        app.task_executors = []
        for agent in app.active_agents:
            executor = task_executor_factory(
                agent,
                task_execution_service.handler_for(agent),
                db_path=app.tasks_db_path,
                job_id=job_id,
            )
            if hasattr(executor, "set_review_eligibility"):
                executor.set_review_eligibility(
                    lambda agent_name=agent: failover_policy.is_operational_review_agent(agent_name)
                )
            if agent in failover_policy.review_agents_for():
                executor.set_review_handler(task_review_service.handler_for(agent))
            executor.start()
            app.task_executors.append(executor)

    def build_tool_executor(self, require_approval_for_mutations: bool = True) -> ToolExecutor:
        """Cria o executor de ferramentas do app com a configuração padrão."""
        app = self.app
        renderer = getattr(app, "renderer", None)
        input_services = getattr(app, "input_services", None)
        input_gate = getattr(app, "input_gate", None)
        base_handler = ConsoleApprovalHandler(
            renderer=renderer,
            suspend_fn=input_services.suspend_nonblocking if input_services else None,
            resume_fn=input_services.resume_nonblocking if input_services else None,
            input_gate=input_gate,
        )
        approval_handler = PreApprovalHandler(base_handler)
        base_handler.set_approve_all_callback(approval_handler.set_approve_all)
        app._approval_handler = approval_handler
        return ToolExecutor(
            config=ToolRuntimeConfig(
                workspace_root=app.workspace.cwd,
                db_path=Path(app.tasks_db_path) if app.tasks_db_path else None,
                require_approval_for_mutations=require_approval_for_mutations,
            ),
            approval_handler=approval_handler,
        )

    def call_agent_for_parallel(self, agent, handoff, protocol_mode, staging_root: Path, index: int):
        """Executa chamada paralela isolando staging por thread.
        Delega para a função global do mesmo módulo."""
        # A função global call_agent_for_parallel está definida abaixo
        # Import explícito para evitar dependência circular em tempo de módulo
        from quimera.app.task import call_agent_for_parallel as _module_fn
        return _module_fn(self.app, agent, handoff, protocol_mode, staging_root, index)

    def stop_task_executors(self):
        """Interrompe executores de tasks em segundo plano."""
        for executor in getattr(self.app, "task_executors", []):
            try:
                executor.stop()
            except KeyboardInterrupt:
                pass
            except Exception:
                pass

    # ── Overview / estado compartilhado ─────────────────────────────────

    def build_task_overview(self) -> dict:
        """Resumo do estado atual das tasks abertas (delega ao repositório)."""
        app = self.app
        repo = self._build_task_repository()
        try:
            job = repo.get_job(app.current_job_id)
            open_tasks = []
            for status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                open_tasks.extend(
                    repo.list_tasks({"job_id": app.current_job_id, "status": status})
                )
            open_tasks.sort(key=lambda task: task["id"])
            counts = {
                "pending": sum(1 for task in open_tasks if task["status"] == TaskStatus.PENDING),
                "in_progress": sum(1 for task in open_tasks if task["status"] == TaskStatus.IN_PROGRESS),
            }
            preview = [
                {
                    "id": task["id"],
                    "status": task["status"],
                    "priority": task.get("priority"),
                    "task_type": task.get("task_type"),
                    "assigned_to": task.get("assigned_to"),
                    "description": task["description"],
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
                "job_id": app.current_job_id,
                "job_description": job["description"] if job else None,
                "open_task_counts": counts,
                "open_tasks_preview": preview,
                "recommended_action": recommended,
            }
        except Exception as exc:
            return {"job_id": app.current_job_id, "error": str(exc)}

    def refresh_task_shared_state(self) -> None:
        """Sincroniza estado compartilhado de tasks no app (delega ao repositório)."""
        app = self.app
        if not hasattr(app, "shared_state") or not isinstance(app.shared_state, dict):
            return
        if not hasattr(app, "current_job_id") or not hasattr(app, "tasks_db_path"):
            return
        app.shared_state["task_overview"] = self.build_task_overview()
        repo = self._build_task_repository()
        completed_tasks = repo.list_tasks(
            {"job_id": app.current_job_id, "status": "completed"}
        )
        if completed_tasks:
            completed_summary = build_completed_task_results(completed_tasks)
            if completed_summary:
                app.shared_state["completed_task_results"] = completed_summary
            else:
                app.shared_state.pop("completed_task_results", None)
        else:
            app.shared_state.pop("completed_task_results", None)

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
        app = self.app
        description = parse_task_command(command)
        if not description:
            app.renderer.show_warning("Uso: /task <descrição>")
            return

        task_type = classify_task_type(description)
        selected_agent = self.choose_agent_with_load_balance(task_type)
        repo = self._build_task_repository()
        task_id = repo.create_task(
            app.current_job_id,
            description,
            task_type=task_type,
            assigned_to=selected_agent,
            origin="human_command",
            status="pending",
            created_by=app.user_name,
            requested_by=app.user_name,
            body=self.build_task_body(description),
            source_context=command,
        )
        self.refresh_task_shared_state()
        lines = [f"task criada com id {task_id}"]
        if selected_agent:
            lines.append(f"atribuída para {selected_agent}")
        lines.append(f"tipo inferido: {task_type}")
        app.system_layer.show_system_message(" | ".join(lines))

    # ── Builders privados ──────────────────────────────────────────────

    def _build_task_repository(self) -> TaskRepository:
        db_path = getattr(self.app, "tasks_db_path", None)
        if not db_path:
            raise ValueError("tasks_db_path is required to access task repository")
        return TaskRepository(db_path)

    def _was_user_cancelled(self) -> bool:
        agent_client = getattr(self.app, "agent_client", None)
        return bool(agent_client and agent_client._user_cancelled)

    def _build_task_prompt_factory(self) -> TaskPromptFactory:
        app = self.app
        return TaskPromptFactory(
            history=getattr(app, "history", None),
            user_name=getattr(app, "user_name", ""),
            shared_state=getattr(app, "shared_state", None),
            prompt_builder=getattr(app, "prompt_builder", None),
        )

    def _build_task_execution_service(self, failover_policy: TaskFailoverPolicy) -> TaskExecutionService:
        app = self.app
        return TaskExecutionService(
            dispatch_services=getattr(app, "dispatch_services", None),
            system_layer=getattr(app, "system_layer", None),
            repository=self._build_task_repository(),
            failover_policy=failover_policy,
            classify_task_execution_result=classify_task_execution_result,
            was_user_cancelled=self._was_user_cancelled,
            record_failure=getattr(app, "record_failure", None),
        )

    def _build_task_review_service(self, failover_policy: TaskFailoverPolicy) -> TaskReviewService:
        app = self.app
        return TaskReviewService(
            dispatch_services=getattr(app, "dispatch_services", None),
            system_layer=getattr(app, "system_layer", None),
            repository=self._build_task_repository(),
            failover_policy=failover_policy,
            classify_task_review_result=classify_task_review_result,
            was_user_cancelled=self._was_user_cancelled,
        )

    def _build_task_router(self) -> TaskRouter:
        app = self.app
        return TaskRouter(
            active_agents=getattr(app, "active_agents", None),
            get_agent_plugin=getattr(app, "get_agent_plugin", lambda _agent_name: None),
            get_available_plugins=getattr(app, "get_available_plugins", lambda: []),
            repository=self._build_task_repository(),
        )

    def _build_task_failover_policy(self) -> TaskFailoverPolicy:
        app = self.app
        return TaskFailoverPolicy(
            active_agents=getattr(app, "active_agents", None),
            get_agent_plugin=getattr(app, "get_agent_plugin", lambda _agent_name: None),
            repository=self._build_task_repository(),
        )


def call_agent_for_parallel(app, agent, handoff, protocol_mode, staging_root: Path, index: int):
    """Executa chamada paralela do agente isolando staging por thread."""
    from ..runtime.tools.files import set_staging_root
    set_staging_root(staging_root / str(index))
    try:
        raw = app.call_agent(agent, handoff=handoff, primary=False, protocol_mode=protocol_mode)
        response, route_target, handoff, extend, needs_input, _ = app.parse_response(raw)
        return agent, response, route_target, handoff, extend, needs_input
    finally:
        set_staging_root(None)
