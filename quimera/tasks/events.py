"""Eventos de domínio para o ciclo de vida de tasks."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ..constants import TaskType


@dataclass(frozen=True, kw_only=True)
class TaskEvent:
    """Evento base — todos os eventos de task herdam daqui."""
    task_id: int
    job_id: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, kw_only=True)
class TaskProposed(TaskEvent):
    """Task foi proposta (status PROPOSED)."""
    description: str
    task_type: TaskType = TaskType.GENERAL
    requested_by: str | None = None
    source_context: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskApproved(TaskEvent):
    """Task foi aprovada para execução."""
    approved_by: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskRejected(TaskEvent):
    """Task foi rejeitada."""
    reason: str | None = None
    rejected_by: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskStarted(TaskEvent):
    """Task iniciou execução (IN_PROGRESS)."""
    assigned_to: str


@dataclass(frozen=True, kw_only=True)
class TaskSubmittedForReview(TaskEvent):
    """Task submetida para revisão (PENDING_REVIEW)."""
    result: str | None = None
    executed_by: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskReviewStarted(TaskEvent):
    """Revisão iniciada (REVIEWING)."""
    reviewed_by: str


@dataclass(frozen=True, kw_only=True)
class TaskCompleted(TaskEvent):
    """Task concluída com sucesso."""
    result: str | None = None
    reviewed_by: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskFailed(TaskEvent):
    """Task falhou sem possibilidade de retentativa."""
    reason: str | None = None
    failed_agent: str | None = None


@dataclass(frozen=True, kw_only=True)
class TaskRequeued(TaskEvent):
    """Task retornou para PENDING após falha (com retentativa)."""
    reason: str | None = None
    failed_agent: str | None = None
    attempt: int = 1


@dataclass(frozen=True, kw_only=True)
class TaskReviewReassigned(TaskEvent):
    """Review foi reatribuído para outro revisor (REVIEWING→PENDING_REVIEW)."""
    reason: str | None = None
    previous_reviewer: str | None = None


@dataclass(frozen=True, kw_only=True)
class BugFiled(TaskEvent):
    """Bug foi arquivado no sistema de rastreamento."""
    bug_id: str
    category: str
    summary: str
    severity: str
