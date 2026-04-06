import os
import sqlite3
from datetime import datetime, timezone

from .task_planning import TASK_TYPE_GENERAL


def get_conn(db_path):
    if not db_path:
        raise ValueError("db_path is required — use workspace.tasks_db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db(db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            created_by TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            body TEXT,
            status TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'general',
            origin TEXT NOT NULL DEFAULT 'legacy',
            assigned_to TEXT,
            result TEXT,
            notes TEXT,
            priority TEXT,
            created_at DATETIME,
            updated_at DATETIME,
            created_by TEXT,
            approved_by TEXT,
            source_context TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
    """)
    cur.execute("PRAGMA table_info(tasks)")
    existing_columns = {row[1] for row in cur.fetchall()}
    column_specs = {
        "task_type": "TEXT NOT NULL DEFAULT 'general'",
        "origin": "TEXT NOT NULL DEFAULT 'legacy'",
        "requested_by": "TEXT",
    }
    for column_name, column_spec in column_specs.items():
        if column_name not in existing_columns:
            cur.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {column_spec}")
    conn.commit()
    conn.close()

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def add_job(description, created_by=None, db_path=None, job_id=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    now = _now()
    if job_id is not None:
        cur.execute("SELECT id FROM jobs WHERE id = ?", (job_id,))
        if cur.fetchone():
            conn.close()
            return job_id
        cur.execute("INSERT INTO jobs(id, description, status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, description, "planning", created_by, now, now))
    else:
        cur.execute("INSERT INTO jobs(description, status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (description, "planning", created_by, now, now))
        job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id

def list_jobs(filt=None, db_path=None):
    filt = filt or {}
    conn = get_conn(db_path)
    cur = conn.cursor()
    sql = "SELECT id, description, status, created_by, created_at, updated_at FROM jobs"
    clauses = []
    params = []
    if "status" in filt:
        clauses.append("status = ?")
        params.append(filt["status"])
    if "created_by" in filt:
        clauses.append("created_by = ?")
        params.append(filt["created_by"])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at ASC, id ASC"
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    conn.close()
    return [{
        "id": r[0],
        "description": r[1],
        "status": r[2],
        "created_by": r[3],
        "created_at": r[4],
        "updated_at": r[5],
    } for r in rows]

def get_job(job_id, db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, description, status, created_by, created_at, updated_at FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "description": row[1],
        "status": row[2],
        "created_by": row[3],
        "created_at": row[4],
        "updated_at": row[5],
    }

def create_task(
    job_id,
    description,
    *,
    task_type=TASK_TYPE_GENERAL,
    assigned_to=None,
    origin="human_command",
    status="pending",
    priority="medium",
    created_by=None,
    requested_by=None,
    notes=None,
    body=None,
    source_context=None,
    db_path=None,
):
    conn = get_conn(db_path)
    cur = conn.cursor()
    now = _now()
    cur.execute("""
        INSERT INTO tasks(
            job_id, description, body, status, task_type, origin, assigned_to,
            priority, created_at, updated_at, created_by, requested_by, notes, source_context
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id, description, body, status, task_type, origin, assigned_to,
        priority, now, now, created_by, requested_by, notes, source_context,
    ))
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id

def propose_task(job_id, description, priority="medium", created_by=None, notes=None, source_context=None, db_path=None, auto_approve=False, body=None):
    status = "approved" if auto_approve else "proposed"
    return create_task(
        job_id,
        description,
        task_type=TASK_TYPE_GENERAL,
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
    conn = get_conn(db_path)
    cur = conn.cursor()
    now = _now()
    cur.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row or row[0] != "proposed":
        conn.close()
        return False
    cur.execute("UPDATE tasks SET status = ?, approved_by = ?, updated_at = ? WHERE id = ?", ("approved", approved_by, now, task_id))
    conn.commit()
    conn.close()
    return True

def reject_task(task_id, rejected_by, reason=None, db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    now = _now()
    cur.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row or row[0] != "proposed":
        conn.close()
        return False
    cur.execute("UPDATE tasks SET status = ?, approved_by = ?, notes = ?, updated_at = ? WHERE id = ?", 
                ("rejected", rejected_by, reason, now, task_id))
    conn.commit()
    conn.close()
    return True

def list_tasks(filt=None, db_path=None):
    filt = filt or {}
    conn = get_conn(db_path)
    cur = conn.cursor()
    sql = (
        "SELECT id, job_id, description, body, status, task_type, origin, assigned_to, "
        "result, notes, priority, created_at, updated_at, created_by, requested_by "
        "FROM tasks"
    )
    clauses = []
    params = []
    if "job_id" in filt:
        clauses.append("job_id = ?")
        params.append(filt["job_id"])
    if "status" in filt:
        clauses.append("status = ?")
        params.append(filt["status"])
    if "assigned_to" in filt:
        clauses.append("assigned_to = ?")
        params.append(filt["assigned_to"])
    if "task_type" in filt:
        clauses.append("task_type = ?")
        params.append(filt["task_type"])
    if "origin" in filt:
        clauses.append("origin = ?")
        params.append(filt["origin"])
    if "id" in filt:
        clauses.append("id = ?")
        params.append(filt["id"])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id ASC"
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    conn.close()
    return [{
        "id": r[0], "job_id": r[1], "description": r[2], "body": r[3], "status": r[4],
        "task_type": r[5], "origin": r[6], "assigned_to": r[7], "result": r[8], "notes": r[9], "priority": r[10],
        "created_at": r[11], "updated_at": r[12], "created_by": r[13], "requested_by": r[14],
    } for r in rows]

def claim_task(agent_name, db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    try:
        # Use a simple transaction to avoid race conditions
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("""
            SELECT id FROM tasks
            WHERE status IN ('pending', 'approved')
              AND (assigned_to = ? OR (status = 'approved' AND assigned_to IS NULL))
            ORDER BY id ASC
            LIMIT 1
        """, (agent_name,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return None
        task_id = row[0]
        cur.execute("UPDATE tasks SET assigned_to = ?, status = ?, updated_at = ? WHERE id = ?",
                    (agent_name, "in_progress", _now(), task_id))
        conn.commit()
        conn.close()
        return task_id
    except Exception:
        conn.rollback()
        conn.close()
        raise

def update_task(task_id, status, result=None, notes=None, db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    now = _now()
    cur.execute("UPDATE tasks SET status = ?, result = ?, notes = ?, updated_at = ? WHERE id = ?",
                (status, result, notes, now, task_id))
    conn.commit()
    conn.close()
    return True

def complete_task(task_id, result=None, db_path=None):
    return update_task(task_id, "completed", result=result, notes=None, db_path=db_path)

def fail_task(task_id, reason=None, db_path=None):
    return update_task(task_id, "failed", result=reason, notes=reason, db_path=db_path)

def drop_db(db_path):
    if os.path.exists(db_path):
        os.remove(db_path)

__all__ = [
    "init_db", "add_job", "list_jobs",
    "create_task", "propose_task", "approve_task", "reject_task", "list_tasks",
    "claim_task", "update_task", "complete_task", "fail_task", "get_job",
]

if __name__ == "__main__":
    import argparse
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
        tid = propose_task(ns.job_id, ns.description, priority=ns.priority, created_by=ns.by, body=ns.body, db_path=ns.db if hasattr(ns, "db") else None)
        print(f"task:{tid}")
        exit(0)
    elif ns.cmd == "approve":
        approve_task(ns.task_id, ns.by, db_path=ns.db if hasattr(ns, "db") else None)
        print("approved")
        exit(0)
    elif ns.cmd == "reject":
        reject_task(ns.task_id, ns.by, reason=ns.reason, db_path=ns.db if hasattr(ns, "db") else None)
        print("rejected")
        exit(0)
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
        update_task(ns.task_id, ns.status, result=ns.result, notes=ns.notes, db_path=ns.db if hasattr(ns, "db") else None)
        print("updated")
        exit(0)
    elif ns.cmd == "complete":
        complete_task(ns.task_id, result=ns.result, db_path=ns.db if hasattr(ns, "db") else None)
        print("completed")
        exit(0)
    else:
        parser.print_help()
        exit(1)
