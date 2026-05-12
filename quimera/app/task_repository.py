"""Repositório de tasks/jobs com ``db_path`` fixo por instância."""

from __future__ import annotations

from ..constants import TaskStatus, TaskType
from ..runtime import tasks as runtime_tasks

_UNSET = object()


class TaskRepository:
    """Wrapper de persistência para CRUD de tasks/jobs."""

    def __init__(self, db_path: str):
        if not db_path:
            raise ValueError("db_path is required")
        self.db_path = db_path

    def create_task(
        self,
        job_id: int,
        description: str,
        *,
        task_type: TaskType | str = TaskType.GENERAL,
        assigned_to: str | None = None,
        origin: str = "human_command",
        status: TaskStatus | str = TaskStatus.PENDING,
        priority: str = "medium",
        created_by: str | None = None,
        requested_by: str | None = None,
        notes: str | None = None,
        body: str | None = None,
        source_context: str | None = None,
    ) -> int:
        """Cria uma task no job informado."""
        return runtime_tasks.create_task(
            job_id,
            description,
            task_type=task_type,
            assigned_to=assigned_to,
            origin=origin,
            status=status,
            priority=priority,
            created_by=created_by,
            requested_by=requested_by,
            notes=notes,
            body=body,
            source_context=source_context,
            db_path=self.db_path,
        )

    def get_job(self, job_id: int) -> dict | None:
        """Retorna um job por ID."""
        return runtime_tasks.get_job(job_id, db_path=self.db_path)

    def list_tasks(self, filt: dict | None = None) -> list[dict]:
        """Lista tasks com filtros opcionais."""
        return runtime_tasks.list_tasks(filt=filt, db_path=self.db_path)

    def fail_task(self, task_id: int, reason: str | None = None) -> bool:
        """Marca task como failed."""
        return runtime_tasks.fail_task(task_id, reason=reason, db_path=self.db_path)

    def requeue_task(self, task_id: int, failed_agent: str, reason: str | None = None) -> bool:
        """Retorna task para pending após falha de execução."""
        return runtime_tasks.requeue_task(
            task_id,
            failed_agent,
            reason=reason,
            db_path=self.db_path,
        )

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool:
        """Conclui task respeitando state machine."""
        kwargs = {
            "result": result,
            "db_path": self.db_path,
        }
        if reviewed_by is not None:
            kwargs["reviewed_by"] = reviewed_by
        return runtime_tasks.complete_task(task_id, **kwargs)

    def submit_for_review(self, task_id: int, result: str | None = None) -> bool:
        """Submete task para review."""
        return runtime_tasks.submit_for_review(task_id, result=result, db_path=self.db_path)

    def requeue_task_after_review(
        self,
        task_id: int,
        failed_agent: str,
        result: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Retorna task para pending após falha no review."""
        return runtime_tasks.requeue_task_after_review(
            task_id,
            failed_agent,
            result=result,
            notes=notes,
            db_path=self.db_path,
        )

    def transition_task(
        self,
        task_id: int,
        to_status: TaskStatus | str,
        *,
        result: str | None | object = _UNSET,
        notes: str | None | object = _UNSET,
        approved_by: str | None | object = _UNSET,
    ) -> bool:
        """Transiciona task para ``to_status`` preservando campos omitidos."""
        kwargs = {"db_path": self.db_path}
        if result is not _UNSET:
            kwargs["result"] = result
        if notes is not _UNSET:
            kwargs["notes"] = notes
        if approved_by is not _UNSET:
            kwargs["approved_by"] = approved_by
        return runtime_tasks.transition_task(task_id, to_status, **kwargs)

    def can_reassign_task(self, task_id: int, candidate_agents: list[str]) -> bool:
        """Retorna True quando algum candidato ainda pode assumir a task."""
        return runtime_tasks.can_reassign_task(task_id, candidate_agents, db_path=self.db_path)
