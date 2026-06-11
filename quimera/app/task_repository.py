"""Repositório de tasks/jobs com ``db_path`` fixo por instância."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from ..constants import TaskStatus, TaskType, can_transition
from ..runtime.models import JobRecord, TaskRecord
from .event_sink import EventSink
from .task_events import (
    TaskProposed, TaskApproved, TaskRejected, TaskStarted,
    TaskSubmittedForReview, TaskReviewStarted, TaskCompleted,
    TaskFailed, TaskRequeued,
)

_UNSET = object()


class TaskRepository:
    """Repositório de persistência para CRUD de tasks/jobs (SQLite)."""

    def __init__(self, db_path: str, event_sink: EventSink | None = None):
        if not db_path:
            raise ValueError("db_path is required")
        self.db_path = db_path
        self.event_sink = event_sink
        self._init_db()

    # ── Infra privada ─────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _publish(self, event: object) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink.publish(event)
            except Exception:
                pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _failed_agents_token(agent_name: str) -> str:
        return f"|{agent_name}|"

    def _init_db(self) -> None:
        conn = self._conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_by  TEXT,
                created_at  DATETIME,
                updated_at  DATETIME,
                started_at  DATETIME,
                completed_at DATETIME
            );
        """)
        cur.execute("PRAGMA table_info(jobs)")
        existing_jobs = {row[1] for row in cur.fetchall()}
        job_migrations = {
            "started_at": "DATETIME",
            "completed_at": "DATETIME",
        }
        for col, spec in job_migrations.items():
            if col not in existing_jobs:
                cur.execute(f"ALTER TABLE jobs ADD COLUMN {col} {spec}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id         INTEGER NOT NULL,
                description    TEXT    NOT NULL,
                body           TEXT,
                status         TEXT    NOT NULL,
                task_type      TEXT    NOT NULL DEFAULT 'general',
                origin         TEXT    NOT NULL DEFAULT 'legacy',
                assigned_to    TEXT,
                result         TEXT,
                notes          TEXT,
                priority       TEXT,
                created_at     DATETIME,
                updated_at     DATETIME,
                started_at     DATETIME,
                completed_at   DATETIME,
                created_by     TEXT,
                approved_by    TEXT,
                source_context TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
            );
        """)
        cur.execute("PRAGMA table_info(tasks)")
        existing = {row[1] for row in cur.fetchall()}
        migrations = {
            "task_type": "TEXT NOT NULL DEFAULT 'general'",
            "origin": "TEXT NOT NULL DEFAULT 'legacy'",
            "requested_by": "TEXT",
            "reviewed_by": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "failed_agents": "TEXT",
            "started_at": "DATETIME",
            "completed_at": "DATETIME",
        }
        for col, spec in migrations.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE tasks ADD COLUMN {col} {spec}")
        conn.commit()
        conn.close()

    # ── Jobs ──────────────────────────────────────────────────────────

    def add_job(self, description: str, created_by: str | None = None, job_id: int | None = None) -> int:
        """Cria um job (ou reutiliza ``job_id`` existente)."""
        conn = self._conn()
        cur = conn.cursor()
        now = self._now()
        try:
            cur.execute("BEGIN IMMEDIATE")
            if job_id is not None:
                cur.execute("SELECT id FROM jobs WHERE id = ?", (job_id,))
                if cur.fetchone():
                    conn.close()
                    return job_id
                cur.execute(
                    "INSERT INTO jobs(id, description, status, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, description, "planning", created_by, now, now),
                )
            else:
                cur.execute(
                    "INSERT INTO jobs(description, status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (description, "planning", created_by, now, now),
                )
                job_id = cur.lastrowid
            conn.commit()
            conn.close()
            return int(job_id)
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def list_jobs(self, filt: dict | None = None) -> list[JobRecord]:
        """Lista jobs com filtros opcionais."""
        filt = filt or {}
        conn = self._conn()
        cur = conn.cursor()
        sql = (
            "SELECT id, description, status, created_by, created_at, updated_at, "
            "started_at, completed_at FROM jobs"
        )
        clauses: list[str] = []
        params: list = []
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
        return [
            JobRecord(
                id=r[0],
                description=r[1],
                status=r[2],
                created_by=r[3],
                created_at=r[4],
                updated_at=r[5],
                started_at=r[6],
                completed_at=r[7],
            )
            for r in rows
        ]

    def get_job(self, job_id: int) -> JobRecord | None:
        """Retorna um job por ID."""
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, description, status, created_by, created_at, updated_at, "
            "started_at, completed_at FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return JobRecord(
            id=row[0],
            description=row[1],
            status=row[2],
            created_by=row[3],
            created_at=row[4],
            updated_at=row[5],
            started_at=row[6],
            completed_at=row[7],
        )

    def update_job_status(self, job_id: int, status: str) -> bool:
        """Atualiza o status de um job."""
        conn = self._conn()
        cur = conn.cursor()
        now = self._now()
        status_text = str(getattr(status, "value", status))
        started_at_sql = ""
        completed_at_sql = ""
        params: list = [status_text, now]
        if status_text in {"active", "in_progress", "completed", "failed", "rejected"}:
            started_at_sql = ", started_at = COALESCE(started_at, ?)"
            params.append(now)
        if status_text in {"completed", "failed", "rejected"}:
            completed_at_sql = ", completed_at = COALESCE(completed_at, ?)"
            params.append(now)
        params.append(job_id)
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                f"UPDATE jobs SET status = ?, updated_at = ?{started_at_sql}{completed_at_sql} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
            affected = cur.rowcount
            conn.close()
            return affected > 0
        except Exception:
            conn.rollback()
            conn.close()
            raise

    # ── Tasks — leitura ───────────────────────────────────────────────

    def list_tasks(self, filt: dict | None = None) -> list[TaskRecord]:
        """Lista tasks com filtros opcionais."""
        filt = filt or {}
        conn = self._conn()
        cur = conn.cursor()
        sql = (
            "SELECT id, job_id, description, body, status, task_type, origin, assigned_to, "
            "result, notes, priority, created_at, updated_at, created_by, requested_by, "
            "started_at, completed_at "
            "FROM tasks"
        )
        clauses: list[str] = []
        params: list = []
        for col in ("job_id", "status", "assigned_to", "task_type", "origin", "id"):
            if col in filt:
                clauses.append(f"{col} = ?")
                params.append(filt[col])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        conn.close()
        return [
            TaskRecord(
                id=r[0], job_id=r[1], description=r[2], body=r[3], status=r[4],
                task_type=r[5], origin=r[6], assigned_to=r[7], result=r[8], notes=r[9],
                priority=r[10], created_at=r[11], updated_at=r[12], created_by=r[13],
                requested_by=r[14], started_at=r[15], completed_at=r[16],
            )
            for r in rows
        ]

    # ── Tasks — escrita ───────────────────────────────────────────────

    def release_agent_tasks(self, agent_name: str) -> None:
        """Libera tasks pendentes/em progresso de um agente para reatribuição."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "UPDATE tasks SET assigned_to = NULL, status = ?, updated_at = ? "
                "WHERE assigned_to = ? AND status IN (?, ?)",
                (TaskStatus.PENDING, self._now(), agent_name, TaskStatus.PENDING, TaskStatus.IN_PROGRESS),
            )
            conn.commit()
            conn.close()
        except Exception:
            conn.rollback()
            conn.close()
            raise

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
        conn = self._conn()
        cur = conn.cursor()
        now = self._now()
        started_at = now if str(status) == TaskStatus.IN_PROGRESS else None
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """
                INSERT INTO tasks(job_id, description, body, status, task_type, origin, assigned_to,
                                  priority, created_at, updated_at, started_at, created_by, requested_by, notes, source_context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, description, body, status, task_type, origin, assigned_to,
                 priority, now, now, started_at, created_by, requested_by, notes, source_context),
            )
            task_id = cur.lastrowid
            conn.commit()
            conn.close()
            self._publish(TaskProposed(
                task_id=int(task_id), job_id=job_id,
                description=description,
                task_type=task_type,
                requested_by=requested_by,
                source_context=source_context,
            ))
            return task_id
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def update_task(
        self,
        task_id: int,
        status: TaskStatus | str,
        result: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Atualiza status/result/notes com timestamps refletindo o progresso."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            now = self._now()
            status_str = status.value if isinstance(status, TaskStatus) else status
            started_at_sql = ""
            completed_at_sql = ""
            if status_str == TaskStatus.IN_PROGRESS.value:
                started_at_sql = ", started_at = COALESCE(started_at, ?)"
            elif status_str in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.REJECTED.value):
                completed_at_sql = ", completed_at = ?"
            params: list = [status, result, notes, now]
            if started_at_sql:
                params.append(now)
            if completed_at_sql:
                params.append(now)
            params.append(task_id)
            cur.execute(
                f"UPDATE tasks SET status = ?, result = ?, notes = ?, updated_at = ?{started_at_sql}{completed_at_sql} WHERE id = ?",
                params,
            )
            conn.commit()
            affected = cur.rowcount
            conn.close()
            return affected > 0
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def fail_task(self, task_id: int, reason: str | None = None) -> bool:
        """Marca task como failed."""
        return self.transition_task(task_id, TaskStatus.FAILED, result=reason, notes=reason)

    def requeue_task(self, task_id: int, failed_agent: str, reason: str | None = None) -> bool:
        """Retorna task para pending após falha de execução."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            now = self._now()
            cur.execute("SELECT status, failed_agents, attempt_count, job_id FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return False
            if not can_transition(row[0], TaskStatus.PENDING):
                conn.rollback()
                conn.close()
                return False
            failed_agents = row[1] or ""
            token = self._failed_agents_token(failed_agent)
            if token not in failed_agents:
                failed_agents += token
            attempt_count = int(row[2] or 0) + 1
            job_id = row[3]
            cur.execute(
                "UPDATE tasks SET status = ?, assigned_to = NULL, result = ?, notes = ?, "
                "failed_agents = ?, attempt_count = ?, started_at = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
                (TaskStatus.PENDING, reason, reason, failed_agents, attempt_count, now, task_id),
            )
            conn.commit()
            conn.close()
            self._publish(TaskRequeued(
                task_id=task_id, job_id=job_id,
                reason=reason, failed_agent=failed_agent, attempt=attempt_count,
            ))
            return True
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def complete_task(self, task_id: int, result: str | None = None, reviewed_by: str | None = None) -> bool:
        """Conclui task respeitando state machine."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT status, job_id FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row or not can_transition(row[0], TaskStatus.COMPLETED):
                conn.rollback()
                conn.close()
                return False
            job_id = row[1]
            now = self._now()
            if reviewed_by:
                cur.execute(
                    "UPDATE tasks SET status = ?, result = ?, reviewed_by = ?, notes = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.COMPLETED, result, reviewed_by, None, now, now, task_id),
                )
            else:
                cur.execute(
                    "UPDATE tasks SET status = ?, result = ?, notes = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.COMPLETED, result, None, now, now, task_id),
                )
            conn.commit()
            conn.close()
            self._publish(TaskCompleted(
                task_id=task_id, job_id=job_id,
                result=result, reviewed_by=reviewed_by,
            ))
            return True
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def submit_for_review(self, task_id: int, result: str | None = None) -> bool:
        """Submete task para review."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT status, job_id, assigned_to FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row or not can_transition(row[0], TaskStatus.PENDING_REVIEW):
                conn.rollback()
                conn.close()
                return False
            job_id, executed_by = row[1], row[2]
            cur.execute(
                "UPDATE tasks SET status = ?, result = ?, notes = ?, reviewed_by = NULL, updated_at = ? WHERE id = ?",
                (TaskStatus.PENDING_REVIEW, result, None, self._now(), task_id),
            )
            conn.commit()
            conn.close()
            self._publish(TaskSubmittedForReview(
                task_id=task_id, job_id=job_id,
                result=result, executed_by=executed_by,
            ))
            return True
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def requeue_task_after_review(
        self,
        task_id: int,
        failed_agent: str,
        result: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Retorna task para pending após falha no review."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            now = self._now()
            cur.execute("SELECT status, failed_agents, attempt_count, job_id FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return False
            if not can_transition(row[0], TaskStatus.PENDING):
                conn.rollback()
                conn.close()
                return False
            failed_agents = row[1] or ""
            token = self._failed_agents_token(failed_agent)
            if token not in failed_agents:
                failed_agents += token
            attempt_count = int(row[2] or 0) + 1
            job_id = row[3]
            cur.execute(
                "UPDATE tasks SET status = ?, assigned_to = NULL, result = ?, notes = ?, "
                "reviewed_by = NULL, failed_agents = ?, attempt_count = ?, "
                "started_at = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
                (TaskStatus.PENDING, result, notes, failed_agents, attempt_count, now, task_id),
            )
            conn.commit()
            conn.close()
            self._publish(TaskRequeued(
                task_id=task_id, job_id=job_id,
                reason=notes, failed_agent=failed_agent, attempt=attempt_count,
            ))
            return True
        except Exception:
            conn.rollback()
            conn.close()
            raise

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
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT status, job_id FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return False
            if not can_transition(row[0], to_status):
                conn.rollback()
                conn.close()
                return False
            job_id = row[1]
            now = self._now()
            fields: dict = {"status": to_status, "updated_at": now}
            if to_status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REJECTED):
                fields["completed_at"] = now
            if result is not _UNSET:
                fields["result"] = result
            if notes is not _UNSET:
                fields["notes"] = notes
            if approved_by is not _UNSET:
                fields["approved_by"] = approved_by
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            cur.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?",
                (*fields.values(), task_id),
            )
            conn.commit()
            conn.close()
            if to_status == TaskStatus.APPROVED:
                self._publish(TaskApproved(
                    task_id=task_id, job_id=job_id,
                    approved_by=fields.get("approved_by"),
                ))
            elif to_status == TaskStatus.REJECTED:
                self._publish(TaskRejected(
                    task_id=task_id, job_id=job_id,
                    reason=fields.get("result"),
                ))
            elif to_status == TaskStatus.FAILED:
                self._publish(TaskFailed(
                    task_id=task_id, job_id=job_id,
                    reason=fields.get("result"),
                ))
            return True
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def claim_task(self, agent_name: str, job_id: int | None = None) -> int | None:
        """Reserva atomicamente uma task pending/approved para o agente."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            job_filter = "AND job_id = ?" if job_id is not None else ""
            params: list = [agent_name, f"%{self._failed_agents_token(agent_name)}%"]
            if job_id is not None:
                params.insert(1, job_id)
            cur.execute(
                f"""
                SELECT id, job_id FROM tasks
                WHERE status IN (?, ?)
                  AND (assigned_to = ? OR assigned_to IS NULL)
                  {job_filter}
                  AND COALESCE(failed_agents, '') NOT LIKE ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (TaskStatus.PENDING, TaskStatus.APPROVED, *params),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return None
            task_id, job_id = row[0], row[1]
            now = self._now()
            cur.execute(
                "UPDATE tasks SET assigned_to = ?, status = ?, started_at = ?, updated_at = ? WHERE id = ?",
                (agent_name, TaskStatus.IN_PROGRESS, now, now, task_id),
            )
            conn.commit()
            conn.close()
            self._publish(TaskStarted(task_id=task_id, job_id=job_id, assigned_to=agent_name))
            return task_id
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def claim_review_task(self, agent_name: str, job_id: int | None = None) -> int | None:
        """Reserva atomicamente uma task pending_review para revisão pelo agente."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            job_filter = "AND job_id = ?" if job_id is not None else ""
            params: list = [agent_name, agent_name]
            if job_id is not None:
                params.append(job_id)
            cur.execute(
                f"""
                SELECT id, job_id FROM tasks
                WHERE status = ?
                  AND (assigned_to IS NULL OR assigned_to != ?)
                  AND (reviewed_by IS NULL OR reviewed_by != ?)
                  {job_filter}
                ORDER BY id ASC
                LIMIT 1
                """,
                (TaskStatus.PENDING_REVIEW, *params),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return None
            task_id, job_id = row[0], row[1]
            cur.execute(
                "UPDATE tasks SET status = ?, reviewed_by = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.REVIEWING, agent_name, self._now(), task_id),
            )
            conn.commit()
            conn.close()
            self._publish(TaskReviewStarted(task_id=task_id, job_id=job_id, reviewed_by=agent_name))
            return task_id
        except Exception:
            conn.rollback()
            conn.close()
            raise

    def can_reassign_task(self, task_id: int, candidate_agents: list[str]) -> bool:
        """Retorna True quando algum candidato ainda pode assumir a task."""
        if not candidate_agents:
            return False
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT failed_agents FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
        except sqlite3.Error:
            conn.close()
            return True
        conn.close()
        if not row:
            return False
        failed_agents = row[0] or ""
        return any(self._failed_agents_token(a) not in failed_agents for a in candidate_agents)
