"""API procedural do domínio de tasks."""
import argparse
import os
import sqlite3
from dataclasses import asdict

from ..constants import TaskStatus, TaskType, can_transition
from .repository import TaskRepository

_UNSET = object()


def get_conn(db_path):
    """Retorna conn."""
    if not db_path:
        raise ValueError("db_path is required — use workspace.tasks_db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path=None):
    """Executa init db."""
    _repository(db_path)


def _repository(db_path):
    if not db_path:
        raise ValueError("db_path is required — use workspace.tasks_db")
    return TaskRepository(str(db_path))


def add_job(description, created_by=None, db_path=None, job_id=None):
    """Executa add job."""
    return _repository(db_path).add_job(description, created_by=created_by, job_id=job_id)


def list_jobs(filt=None, db_path=None):
    """Lista jobs."""
    return [asdict(job) for job in _repository(db_path).list_jobs(filt or {})]


def get_job(job_id, db_path=None):
    """Retorna job."""
    job = _repository(db_path).get_job(job_id)
    return asdict(job) if job else None


def update_job_status(job_id, status, db_path=None):
    """Atualiza o status de um job."""
    return _repository(db_path).update_job_status(job_id, status)


def create_task(
        job_id,
        description,
        *,
        task_type=TaskType.GENERAL,
        assigned_to=None,
        origin="human_command",
        status=TaskStatus.PENDING,
        priority="medium",
        created_by=None,
        requested_by=None,
        notes=None,
        body=None,
        source_context=None,
        db_path=None,
):
    """Cria task."""
    return _repository(db_path).create_task(
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
    )


def propose_task(job_id, description, priority="medium", created_by=None, notes=None, source_context=None, db_path=None,
                 auto_approve=False, body=None):
    """Executa propose task."""
    status = TaskStatus.APPROVED if auto_approve else TaskStatus.PROPOSED
    return create_task(
        job_id,
        description,
        task_type=TaskType.GENERAL,
        origin="legacy_tool",
        status=status,
        priority=priority,
        created_by=created_by,
        notes=notes,
        body=body,
        source_context=source_context,
        db_path=db_path,
    )


def approve_task(task_id, approved_by, db_path=None):
    """Aprova task."""
    return _repository(db_path).transition_task(
        task_id,
        TaskStatus.APPROVED,
        approved_by=approved_by,
    )


def reject_task(task_id, rejected_by, reason=None, db_path=None):
    """Rejeita task."""
    return _repository(db_path).transition_task(
        task_id,
        TaskStatus.REJECTED,
        approved_by=rejected_by,
        notes=reason,
    )


def list_tasks(filt=None, db_path=None):
    """Lista tasks."""
    return [asdict(task) for task in _repository(db_path).list_tasks(filt or {})]


def release_agent_tasks(agent_name, db_path=None):
    """Release tasks from a failed agent so others can pick them up.

    Notably, also reset the status to 'pending' so tasks can be claimed again
    by other agents. Previously, tasks could be left in 'in_progress' state
    after release, making them unclaimable by the router.
    """
    _repository(db_path).release_agent_tasks(agent_name)


def requeue_task(task_id, failed_agent, reason=None, db_path=None):
    """Release a task after an execution failure so another agent can claim it."""
    return _repository(db_path).requeue_task(task_id, failed_agent, reason=reason)


def requeue_task_after_review(task_id, failed_agent, result=None, notes=None, db_path=None):
    """Return a reviewed task to pending and force execution failover to another agent."""
    return _repository(db_path).requeue_task_after_review(
        task_id,
        failed_agent,
        result=result,
        notes=notes,
    )


def can_reassign_task(task_id, candidate_agents, db_path=None):
    """Return True when at least one candidate agent can still claim the task."""
    try:
        return _repository(db_path).can_reassign_task(task_id, candidate_agents)
    except sqlite3.Error:
        return True


def claim_task(agent_name, job_id=None, db_path=None):
    """Reserva task."""
    return _repository(db_path).claim_task(agent_name, job_id=job_id)


def update_task(task_id, status, result=None, notes=None, db_path=None):
    """Atualiza task."""
    return _repository(db_path).update_task(task_id, status, result=result, notes=notes)


def complete_task(task_id, result=None, reviewed_by=None, db_path=None):
    """Conclui task, validando a transição para COMPLETED atomicamente."""
    return _repository(db_path).complete_task(task_id, result=result, reviewed_by=reviewed_by)


def fail_task(task_id, reason=None, db_path=None):
    """Marca como falha task, validando a transição para FAILED via state machine."""
    return transition_task(task_id, TaskStatus.FAILED, result=reason, notes=reason, db_path=db_path)


def submit_for_review(task_id, result=None, db_path=None):
    """Submete task para review, validando a transição para PENDING_REVIEW atomicamente."""
    return _repository(db_path).submit_for_review(task_id, result=result)


def claim_review_task(agent_name, job_id=None, db_path=None):
    """Atomically claim a pending_review task executed and not already reviewed by this agent."""
    return _repository(db_path).claim_review_task(agent_name, job_id=job_id)


def transition_task(task_id, to_status, result=_UNSET, notes=_UNSET, approved_by=_UNSET, db_path=None):
    """Transiciona uma task para to_status validando a transição via can_transition().

    Retorna True em sucesso, False se a task não existir ou a transição for inválida.
    A operação é atômica: leitura e escrita ocorrem na mesma conexão.
    Colunas não fornecidas (sentinela _UNSET) são preservadas — não são sobrescritas.
    """
    kwargs = {}
    if result is not _UNSET:
        kwargs["result"] = result
    if notes is not _UNSET:
        kwargs["notes"] = notes
    if approved_by is not _UNSET:
        kwargs["approved_by"] = approved_by
    return _repository(db_path).transition_task(task_id, to_status, **kwargs)


def drop_db(db_path):
    """Remove db."""
    if os.path.exists(db_path):
        os.remove(db_path)


__all__ = [
    "init_db", "add_job", "list_jobs",
    "create_task", "propose_task", "approve_task", "reject_task", "list_tasks",
    "claim_task", "release_agent_tasks", "update_task", "complete_task", "fail_task", "get_job", "update_job_status",
    "submit_for_review", "claim_review_task", "requeue_task_after_review",
    "transition_task",
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="quimera-tasks", description="Stage 5: planning tasks (jobs + tasks).")
    parser.add_argument("--db", dest="db", required=True, help="Path to tasks DB")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="Initialize the DB")
    p_init.add_argument("--db", dest="db", help="DB path")

    p_job = sub.add_parser("add-job", help="Add a new job")
    p_job.add_argument("description")
    p_job.add_argument("--by", dest="by", default=None)

    p_job_list = sub.add_parser("list-jobs", help="List jobs")
    p_job_list.add_argument("--status", dest="status", default=None)

    p_task = sub.add_parser("propose", help="Propose a new task for a job")
    p_task.add_argument("--job-id", dest="job_id", type=int)
    p_task.add_argument("--desc", dest="description")
    p_task.add_argument("--body", dest="body", default=None)
    p_task.add_argument("--priority", default="medium")
    p_task.add_argument("--by", dest="by", default=None)

    p_task_approve = sub.add_parser("approve", help="Approve a task")
    p_task_approve.add_argument("--id", dest="task_id", type=int)
    p_task_approve.add_argument("--by", dest="by", default=None)

    p_task_reject = sub.add_parser("reject", help="Reject a task")
    p_task_reject.add_argument("--id", dest="task_id", type=int, required=True)
    p_task_reject.add_argument("--by", dest="by", default=None)
    p_task_reject.add_argument("--reason", dest="reason", default=None)

    p_task_list = sub.add_parser("list-tasks", help="List tasks")
    p_task_list.add_argument("--job-id", dest="job_id", type=int, default=None)
    p_task_list.add_argument("--status", dest="status", default=None)

    p_task_claim = sub.add_parser("claim", help="Claim a task for an agent")
    p_task_claim.add_argument("--agent", dest="agent", required=True)

    p_task_update = sub.add_parser("update", help="Update a task status")
    p_task_update.add_argument("--id", dest="task_id", type=int, required=True)
    p_task_update.add_argument("--status", dest="status", required=True)
    p_task_update.add_argument("--result", dest="result", default=None)
    p_task_update.add_argument("--notes", dest="notes", default=None)

    p_task_complete = sub.add_parser("complete", help="Mark a task as completed")
    p_task_complete.add_argument("--id", dest="task_id", type=int, required=True)
    p_task_complete.add_argument("--result", dest="result", default=None)

    ns = parser.parse_args()
    init_db(ns.db)
    if ns.cmd == "init":
        print("DB initialized.")
        exit(0)
    elif ns.cmd == "add-job":
        job_id = add_job(ns.description, created_by=ns.by, db_path=ns.db if hasattr(ns, "db") else None)
        print(f"job:{job_id}")
        exit(0)
    elif ns.cmd == "list-jobs":
        for j in list_jobs({"status": ns.status}, db_path=ns.db if hasattr(ns, "db") else None):
            print(j)
        exit(0)
    elif ns.cmd == "propose":
        tid = propose_task(ns.job_id, ns.description, priority=ns.priority, created_by=ns.by, body=ns.body,
                           db_path=ns.db if hasattr(ns, "db") else None)
        print(f"task:{tid}")
        exit(0)
    elif ns.cmd == "approve":
        ok = transition_task(ns.task_id, TaskStatus.APPROVED,
                             approved_by=ns.by,
                             db_path=ns.db if hasattr(ns, "db") else None)
        print("approved" if ok else "transition-failed")
        exit(0 if ok else 1)
    elif ns.cmd == "reject":
        ok = transition_task(ns.task_id, TaskStatus.REJECTED,
                             approved_by=ns.by, notes=ns.reason,
                             db_path=ns.db if hasattr(ns, "db") else None)
        print("rejected" if ok else "transition-failed")
        exit(0 if ok else 1)
    elif ns.cmd == "list-tasks":
        for t in list_tasks({"job_id": ns.job_id, "status": ns.status}, db_path=ns.db if hasattr(ns, "db") else None):
            print(t)
        exit(0)
    elif ns.cmd == "claim":
        t = claim_task(ns.agent, db_path=ns.db if hasattr(ns, "db") else None)
        if t:
            print(f"claimed:{t}")
        else:
            print("no-tasks-available")
        exit(0)
    elif ns.cmd == "update":
        ok = transition_task(ns.task_id, ns.status, result=ns.result, notes=ns.notes,
                             db_path=ns.db if hasattr(ns, "db") else None)
        print("updated" if ok else "transition-failed")
        exit(0 if ok else 1)
    elif ns.cmd == "complete":
        ok = transition_task(ns.task_id, TaskStatus.COMPLETED, result=ns.result,
                             db_path=ns.db if hasattr(ns, "db") else None)
        print("completed" if ok else "transition-failed")
        exit(0 if ok else 1)
    else:
        parser.print_help()
        exit(1)
