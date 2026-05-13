"""Review de tasks com transições explícitas e fallback operacional."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..runtime.task_reviewer import TaskReviewer
from ..runtime.models import TaskRecord
from .task_repository import TaskRepository


class _DispatchServicesProto(Protocol):
    """Interface mínima de dispatch usada pelo serviço de review."""

    def call_agent(self, *args, **kwargs) -> str | None: ...


class _SystemLayerProto(Protocol):
    """Interface mínima de saída para mensagens de sistema."""

    def show_muted_message(self, message: str) -> None: ...


class _TaskRepositoryProto(Protocol):
    """Interface mínima de persistência usada no review."""

    def transition_task(self, task_id: int, to_status: str, *, result=None, notes=None, approved_by=None) -> bool: ...

    def requeue_task_after_review(self, task_id: int, failed_agent: str, result=None, notes=None) -> bool: ...

    def complete_task(self, task_id: int, result=None, reviewed_by=None) -> bool: ...

    def fail_task(self, task_id: int, reason=None) -> bool: ...


class _FailoverPolicyProto(Protocol):
    """Interface mínima de política de failover/review."""

    def has_review_failover(self, executor_agent: str | None, failed_reviewer: str) -> bool: ...


class TaskReviewService:
    """Adaptador que expõe handler compatível com TaskExecutor via TaskReviewer."""

    def __init__(
        self,
        dispatch_services: _DispatchServicesProto,
        system_layer: _SystemLayerProto,
        repository: TaskRepository | _TaskRepositoryProto,
        failover_policy: _FailoverPolicyProto,
        classify_task_review_result: Callable[[str | None], tuple[bool, str, str]],
        was_user_cancelled: Callable[[], bool],
        event_sink: object | None = None,
    ) -> None:
        self._reviewer = TaskReviewer(
            dispatch_services=dispatch_services,
            system_layer=system_layer,
            repository=repository,
            failover_policy=failover_policy,
            classify_task_review_result=classify_task_review_result,
            was_user_cancelled=was_user_cancelled,
            event_sink=event_sink,
        )

    def handler_for(self, agent_name: str) -> Callable[[TaskRecord], bool]:
        """Retorna handler de review que delega ao TaskReviewer."""
        return lambda task: self._reviewer.review(task, agent_name)
