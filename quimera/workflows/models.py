"""Modelos tipados do domínio de workflows distribuídos."""
from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field, fields

from ..domain.work_items import WorkItem


class WorkflowStatus(str, enum.Enum):
    """Estado global de um workflow; os estados de task continuam os de ``TaskStatus``."""

    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Estados em que o workflow ainda pode progredir (não terminais).
OPEN_WORKFLOW_STATES = (
    WorkflowStatus.READY,
    WorkflowStatus.RUNNING,
    WorkflowStatus.PAUSED,
    WorkflowStatus.BLOCKED,
)

WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.READY: frozenset({WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED}),
    WorkflowStatus.RUNNING: frozenset({
        WorkflowStatus.PAUSED,
        WorkflowStatus.BLOCKED,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    }),
    WorkflowStatus.PAUSED: frozenset({WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED}),
    WorkflowStatus.BLOCKED: frozenset({
        WorkflowStatus.RUNNING,
        WorkflowStatus.PAUSED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    }),
    # Reabrir um workflow concluído só ocorre por replanejamento do coordenador.
    WorkflowStatus.COMPLETED: frozenset({WorkflowStatus.RUNNING}),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}

TERMINAL_WORKFLOW_STATUSES = frozenset({
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
})


def can_transition_workflow(
    from_status: WorkflowStatus | str, to_status: WorkflowStatus | str
) -> bool:
    """True quando a transição de estado de workflow é permitida."""
    try:
        source, target = WorkflowStatus(from_status), WorkflowStatus(to_status)
    except ValueError:
        return False
    return target in WORKFLOW_TRANSITIONS[source]


class WorkflowError(Exception):
    """Falha de domínio de workflow (estado inválido, limite excedido, inexistente)."""


class WorkflowNotFound(WorkflowError):
    """Workflow inexistente."""


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    """Limites operacionais que impedem cascatas e loops indefinidos."""

    max_tasks: int = 20
    max_replans: int = 3
    max_consults: int = 6
    max_open_workflows: int = 3
    max_task_attempts: int = 3
    lease_seconds: int = 1800

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> WorkflowLimits:
        """Aplica sobre os padrões só as chaves conhecidas com inteiros positivos."""
        known = {field.name for field in fields(cls)}
        return cls(**{
            key: value
            for key, value in (values or {}).items()
            if key in known and type(value) is int and value > 0
        })


@dataclass(frozen=True, slots=True)
class WorkflowTaskSpec:
    """Uma tarefa do plano: o ``WorkItem`` comum mais o que é específico do workflow."""

    item: WorkItem
    evidence_expected: tuple[str, ...] = ()
    writes: bool = False

    @property
    def id(self) -> str:
        return self.item.id


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """Contrato estruturado de um plano (uma versão)."""

    objective: str
    coordinator: str
    tasks: tuple[WorkflowTaskSpec, ...]
    global_criteria: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    version: int = 1


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    """Linha persistida de um workflow."""

    id: int
    job_id: int
    objective: str
    coordinator: str
    status: WorkflowStatus
    plan_version: int
    global_criteria: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    replans: int = 0
    consults: int = 0
    status_reason: str | None = None
    attention: str | None = None
    result: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTaskLink:
    """Vínculo item do plano ↔ task persistida."""

    workflow_id: int
    item_id: str
    task_id: int
    plan_version: int
    writes: bool = False
    evidence_expected: tuple[str, ...] = ()
    superseded_in: int | None = None


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """Tentativa individual de execução de uma task."""

    task_id: int
    attempt: int
    agent: str
    status: str
    run_id: str | None = None
    failure_reason: str | None = None
    handoff: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskView:
    """Visão derivada do banco de uma task do workflow, para decisões e UI."""

    link: WorkflowTaskLink
    status: str
    assigned_to: str | None
    attempt_count: int
    title: str
    dependencies: tuple[str, ...] = ()
    result: str | None = None
    notes: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """Estado completo e reconstruível a partir do banco."""

    workflow: WorkflowRecord
    tasks: tuple[TaskView, ...] = field(default_factory=tuple)

    def counts(self) -> dict[str, int]:
        """Contagens por categoria operacional (somente tasks vigentes)."""
        counts = {"total": len(self.tasks), "completed": 0, "running": 0, "pending": 0,
                  "blocked": 0, "failed": 0, "cancelled": 0}
        by_item = {view.link.item_id: view for view in self.tasks}
        for view in self.tasks:
            if view.status == "completed":
                counts["completed"] += 1
            elif view.status in {"in_progress", "pending_review", "reviewing"}:
                counts["running"] += 1
            elif view.status in {"failed", "rejected"}:
                counts["failed"] += 1
            elif view.status == "cancelled":
                counts["cancelled"] += 1
            elif any(
                by_item[dep].status != "completed"
                for dep in view.dependencies
                if dep in by_item
            ):
                counts["blocked"] += 1
            else:
                counts["pending"] += 1
        return counts
