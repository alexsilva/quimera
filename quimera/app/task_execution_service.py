"""Execução de tasks com políticas explícitas de review e failover."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..runtime.task_runner import TaskRunner
from ..runtime.models import TaskRecord
from .task_repository import TaskRepository


class _DispatchServicesProto(Protocol):
    """Interface mínima de dispatch usada pelo executor de tasks."""

    def call_agent(self, *args, **kwargs) -> str | None: ...


class _SystemLayerProto(Protocol):
    """Interface mínima de saída para mensagens de sistema."""

    def show_muted_message(self, message: str) -> None: ...


class _TaskRepositoryProto(Protocol):
    """Interface mínima de persistência usada na execução."""

    def fail_task(self, task_id: int, reason: str | None = None) -> bool: ...

    def requeue_task(self, task_id: int, failed_agent: str, reason: str | None = None) -> bool: ...

    def submit_for_review(self, task_id: int, result: str | None = None) -> bool: ...

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool: ...


class _FailoverPolicyProto(Protocol):
    """Interface mínima de política de failover/review."""

    def review_agents_for(
        self,
        executor_agent: str | None = None,
        exclude_agents: set[str] | None = None,
    ) -> list[str]: ...

    def can_failover(self, task_id: int, failed_agent: str) -> bool: ...


class TaskExecutionService:
    """Adaptador que expõe handler compatível com TaskExecutor via TaskRunner."""

    def __init__(
        self,
        dispatch_services: _DispatchServicesProto,
        system_layer: _SystemLayerProto,
        repository: TaskRepository | _TaskRepositoryProto,
        failover_policy: _FailoverPolicyProto,
        classify_task_execution_result: Callable[[str | None], tuple[bool, str]],
        was_user_cancelled: Callable[[], bool],
        record_failure: Callable[[str], None] | None = None,
        before_agent_call: Callable[[str], None] | None = None,
        after_agent_call: Callable[[str], None] | None = None,
    ) -> None:
        self._runner = TaskRunner(
            dispatch_services=dispatch_services,
            system_layer=system_layer,
            repository=repository,
            failover_policy=failover_policy,
            classify_task_execution_result=classify_task_execution_result,
            was_user_cancelled=was_user_cancelled,
            record_failure=record_failure,
            before_agent_call=before_agent_call,
            after_agent_call=after_agent_call,
        )

    def handler_for(self, agent_name: str) -> Callable[[TaskRecord], bool]:
        """Retorna handler de execução que delega ao TaskRunner."""
        return lambda task: self._runner.run(task, agent_name)
