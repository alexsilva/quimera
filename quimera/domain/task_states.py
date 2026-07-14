"""Estados, tipos e transições de tarefas do domínio Quimera."""
from __future__ import annotations

import enum


class Visibility(str, enum.Enum):
    """Nível de visibilidade da execução do agente."""
    QUIET = "quiet"
    SUMMARY = "summary"
    FULL = "full"


class TaskStatus(str, enum.Enum):
    """Status de uma tarefa no domínio do Quimera."""
    PENDING = "pending"
    PROPOSED = "proposed"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


# Tabela de transições válidas entre estados de task
VALID_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PROPOSED: frozenset({TaskStatus.APPROVED, TaskStatus.REJECTED}),
    TaskStatus.APPROVED: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.PENDING: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.IN_PROGRESS: frozenset({
        TaskStatus.PENDING_REVIEW,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.PENDING,
    }),
    TaskStatus.PENDING_REVIEW: frozenset({TaskStatus.REVIEWING}),
    TaskStatus.REVIEWING: frozenset({
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.PENDING,
        TaskStatus.PENDING_REVIEW,
    }),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
}


def can_transition(from_status: TaskStatus | str, to_status: TaskStatus | str) -> bool:
    """Retorna True se a transição de from_status para to_status é válida."""
    try:
        from_s = TaskStatus(from_status)
        to_s = TaskStatus(to_status)
    except ValueError:
        return False
    return to_s in VALID_TRANSITIONS.get(from_s, frozenset())


class TaskType(str, enum.Enum):
    """Tipos de tarefas suportados para classificação e roteamento."""
    TEST_EXECUTION = "test_execution"
    CODE_REVIEW = "code_review"
    CODE_EDIT = "code_edit"
    BUG_INVESTIGATION = "bug_investigation"
    ARCHITECTURE = "architecture"
    DOCUMENTATION = "documentation"
    GENERAL = "general"
