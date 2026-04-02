import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from ..workspace import QUIMERA_BASE

"""
DB path resolution for Stage 5 tasks.
Priority:
- QUIMERA_TASKS_DB env var
- QUIMERA_WORKFLOW_ROOT env var (expects storage/tasks.db under this root)
- Default: ~/.local/share/quimera/tasks.db
"""

def _default_tasks_db_path() -> Path:
    return QUIMERA_BASE / "tasks.db"

def _resolve_tasks_db_path() -> Path:
    env_db = os.environ.get("QUIMERA_TASKS_DB")
    if env_db:
        return Path(env_db).expanduser().resolve()
    wf_root = os.environ.get("QUIMERA_WORKFLOW_ROOT")
    if wf_root:
        return Path(wf_root) / "storage" / "tasks.db"
    return _default_tasks_db_path()

# Resolve path at import time
DB_PATH = _resolve_tasks_db_path()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def get_conn(db_path=None):
    path = db_path or DB_PATH
    # ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
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

def propose_task(job_id, description, priority="medium", created_by=None, notes=None, source_context=None, db_path=None, auto_approve=False, body=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    now = _now()
    status = "approved" if auto_approve else "proposed"
    cur.execute("""
        INSERT INTO tasks(job_id, description, body, status, priority, created_at, updated_at, created_by, notes, source_context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, description, body, status, priority, now, now, created_by, notes, source_context))
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id

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
    sql = "SELECT id, job_id, description, body, status, assigned_to, result, notes, priority, created_at, updated_at FROM tasks"
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
        "assigned_to": r[5], "result": r[6], "notes": r[7], "priority": r[8],
        "created_at": r[9], "updated_at": r[10]
    } for r in rows]

def claim_task(agent_name, db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()
    try:
        # Use a simple transaction to avoid race conditions
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("""
            SELECT id FROM tasks
            WHERE status = 'approved' AND assigned_to IS NULL
            ORDER BY id ASC
            LIMIT 1
        """)
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

def drop_db(db_path=None):
    p = db_path or DB_PATH
    if os.path.exists(p):
        os.remove(p)

__all__ = [
    "init_db", "add_job", "list_jobs",
    "propose_task", "approve_task", "reject_task", "list_tasks",
    "claim_task", "update_task", "complete_task", "fail_task", "get_job",
]

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="quimera-tasks", description="Stage 5: planning tasks (jobs + tasks).")
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
    init_db(DB_PATH)
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
