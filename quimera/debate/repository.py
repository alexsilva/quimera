"""SQLite persistence for debate sessions and their audit trail."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .models import (
    DebateContribution,
    DebateEvidence,
    DebateLimits,
    DebateMode,
    DebateSession,
    DebateStatus,
    DebateSynthesis,
    TERMINAL_DEBATE_STATUSES,
    WorkItem,
    synthesis_from_dict,
)


class DebateRepository:
    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValueError("db_path is required")
        self.db_path = str(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS debates (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    participants_json TEXT NOT NULL,
                    moderator TEXT NOT NULL,
                    max_rounds INTEGER NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    quorum INTEGER NOT NULL,
                    current_round INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    started_at DATETIME,
                    completed_at DATETIME,
                    applied_at DATETIME
                );

                CREATE TABLE IF NOT EXISTS debate_contributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    debate_id TEXT NOT NULL,
                    round_index INTEGER NOT NULL,
                    agent TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    raw_response TEXT,
                    created_at DATETIME NOT NULL,
                    UNIQUE(debate_id, round_index, agent),
                    FOREIGN KEY(debate_id) REFERENCES debates(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS debate_syntheses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    debate_id TEXT NOT NULL,
                    round_index INTEGER NOT NULL,
                    moderator TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    raw_response TEXT,
                    created_at DATETIME NOT NULL,
                    UNIQUE(debate_id, round_index),
                    FOREIGN KEY(debate_id) REFERENCES debates(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS debate_work_items (
                    debate_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(debate_id, item_id),
                    FOREIGN KEY(debate_id) REFERENCES debates(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS debate_task_links (
                    debate_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    PRIMARY KEY(debate_id, item_id),
                    UNIQUE(task_id),
                    FOREIGN KEY(debate_id) REFERENCES debates(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_debates_updated
                    ON debates(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_debate_contributions_round
                    ON debate_contributions(debate_id, round_index, id);
            """)
            # Debate task creation is retry-safe across concurrent app instances.
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_debate_source "
                    "ON tasks(source_context) WHERE origin = 'debate'"
                )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise

    def create_session(self, session: DebateSession) -> None:
        now = self._now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO debates(
                    id, session_id, topic, mode, status, participants_json,
                    moderator, max_rounds, timeout_seconds, quorum,
                    current_round, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.session_id,
                    session.topic,
                    session.mode.value,
                    session.status.value,
                    json.dumps(session.participants, ensure_ascii=False),
                    session.moderator,
                    session.limits.max_rounds,
                    session.limits.timeout_seconds,
                    session.limits.quorum,
                    session.current_round,
                    session.created_at or now,
                    session.updated_at or now,
                ),
            )

    def get_session(self, debate_id: str) -> DebateSession | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM debates WHERE id = ?", (debate_id,)
            ).fetchone()
        return self._session_from_row(row) if row else None

    def latest_session(self) -> DebateSession | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM debates ORDER BY updated_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self, *, limit: int = 20) -> list[DebateSession]:
        safe_limit = min(100, max(1, int(limit)))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM debates ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def set_status(
        self,
        debate_id: str,
        status: DebateStatus,
        *,
        current_round: int | None = None,
        error: str | None = None,
        result: DebateSynthesis | None = None,
    ) -> None:
        now = self._now()
        terminal = status in TERMINAL_DEBATE_STATUSES
        fields = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, now]
        if current_round is not None:
            fields.append("current_round = ?")
            params.append(int(current_round))
        if error is not None:
            fields.append("error = ?")
            params.append(str(error))
        if result is not None:
            fields.append("result_json = ?")
            params.append(json.dumps(result.as_dict(), ensure_ascii=False))
        if status in {DebateStatus.RUNNING, DebateStatus.SYNTHESIZING}:
            fields.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if terminal:
            fields.append("completed_at = COALESCE(completed_at, ?)")
            params.append(now)
        params.append(debate_id)
        with self._conn() as conn:
            cursor = conn.execute(
                f"UPDATE debates SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"debate inexistente: {debate_id}")

    def add_contribution(self, contribution: DebateContribution) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO debate_contributions(
                    debate_id, round_index, agent, payload_json, raw_response, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(debate_id, round_index, agent) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    raw_response = excluded.raw_response,
                    created_at = excluded.created_at
                """,
                (
                    contribution.debate_id,
                    contribution.round_index,
                    contribution.agent,
                    json.dumps(contribution.as_dict(), ensure_ascii=False),
                    contribution.raw_response,
                    self._now(),
                ),
            )

    def list_contributions(
        self,
        debate_id: str,
        *,
        round_index: int | None = None,
    ) -> list[DebateContribution]:
        sql = "SELECT * FROM debate_contributions WHERE debate_id = ?"
        params: list[Any] = [debate_id]
        if round_index is not None:
            sql += " AND round_index = ?"
            params.append(round_index)
        sql += " ORDER BY round_index ASC, id ASC"
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        contributions: list[DebateContribution] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            contributions.append(
                DebateContribution(
                    debate_id=payload["debate_id"],
                    round_index=int(payload["round_index"]),
                    agent=payload["agent"],
                    position=payload["position"],
                    arguments=tuple(payload.get("arguments") or ()),
                    objections=tuple(payload.get("objections") or ()),
                    proposal=payload.get("proposal") or "",
                    confidence=float(payload.get("confidence") or 0.0),
                    vote=payload.get("vote") or "abstain",
                    critical_objection=bool(payload.get("critical_objection", False)),
                    work_items=tuple(
                        WorkItem(**_work_item_kwargs(item))
                        for item in payload.get("work_items") or ()
                    ),
                    evidence=tuple(
                        DebateEvidence(**_evidence_kwargs(item))
                        for item in payload.get("evidence") or ()
                    ),
                    evidence_ids=tuple(payload.get("evidence_ids") or ()),
                    raw_response=row["raw_response"] or "",
                )
            )
        return contributions

    def add_synthesis(self, synthesis: DebateSynthesis) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO debate_syntheses(
                    debate_id, round_index, moderator, payload_json, raw_response, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(debate_id, round_index) DO UPDATE SET
                    moderator = excluded.moderator,
                    payload_json = excluded.payload_json,
                    raw_response = excluded.raw_response,
                    created_at = excluded.created_at
                """,
                (
                    synthesis.debate_id,
                    synthesis.round_index,
                    synthesis.moderator,
                    json.dumps(synthesis.as_dict(), ensure_ascii=False),
                    synthesis.raw_response,
                    self._now(),
                ),
            )
        self.replace_work_items(synthesis.debate_id, synthesis.work_items)

    def list_syntheses(self, debate_id: str) -> list[DebateSynthesis]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json, raw_response FROM debate_syntheses "
                "WHERE debate_id = ? ORDER BY round_index ASC",
                (debate_id,),
            ).fetchall()
        result: list[DebateSynthesis] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["raw_response"] = row["raw_response"] or ""
            synthesis = synthesis_from_dict(payload)
            if synthesis is not None:
                result.append(synthesis)
        return result

    def replace_work_items(self, debate_id: str, items: tuple[WorkItem, ...]) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM debate_work_items WHERE debate_id = ?", (debate_id,)
            )
            conn.executemany(
                "INSERT INTO debate_work_items(debate_id, item_id, ordinal, payload_json) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        debate_id,
                        item.id,
                        index,
                        json.dumps(item.as_dict(), ensure_ascii=False),
                    )
                    for index, item in enumerate(items)
                ],
            )

    def get_work_items(self, debate_id: str) -> tuple[WorkItem, ...]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM debate_work_items "
                "WHERE debate_id = ? ORDER BY ordinal ASC",
                (debate_id,),
            ).fetchall()
        return tuple(
            WorkItem(**_work_item_kwargs(json.loads(row["payload_json"])))
            for row in rows
        )

    def link_task(self, debate_id: str, item_id: str, task_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO debate_task_links(debate_id, item_id, task_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (debate_id, item_id, int(task_id), self._now()),
            )

    def task_links(self, debate_id: str) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT item_id, task_id FROM debate_task_links WHERE debate_id = ?",
                (debate_id,),
            ).fetchall()
        return {str(row["item_id"]): int(row["task_id"]) for row in rows}

    def mark_applied(self, debate_id: str) -> None:
        now = self._now()
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE debates SET applied_at = COALESCE(applied_at, ?), updated_at = ? "
                "WHERE id = ?",
                (now, now, debate_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"debate inexistente: {debate_id}")

    def recover_incomplete(
        self, *, reason: str = "interrompido pelo encerramento anterior"
    ) -> int:
        now = self._now()
        active = (
            DebateStatus.CREATED.value,
            DebateStatus.RUNNING.value,
            DebateStatus.SYNTHESIZING.value,
        )
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE debates SET status = ?, error = ?, updated_at = ?, completed_at = ? "
                "WHERE status IN (?, ?, ?)",
                (DebateStatus.FAILED.value, reason, now, now, *active),
            )
            return int(cursor.rowcount)

    def recover_expired(
        self,
        *,
        reason: str = "debate interrompido apos exceder o timeout total",
        now: str | None = None,
    ) -> int:
        """Fail only expired active debates, preserving other live app instances."""
        recovered_at = now or self._now()
        active = (
            DebateStatus.CREATED.value,
            DebateStatus.RUNNING.value,
            DebateStatus.SYNTHESIZING.value,
        )
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE debates SET status = ?, error = ?, updated_at = ?, completed_at = ? "
                "WHERE status IN (?, ?, ?) "
                "AND julianday(COALESCE(started_at, created_at)) "
                "+ (timeout_seconds / 86400.0) <= julianday(?)",
                (
                    DebateStatus.FAILED.value,
                    reason,
                    recovered_at,
                    recovered_at,
                    *active,
                    recovered_at,
                ),
            )
            return int(cursor.rowcount)

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> DebateSession:
        result_payload = json.loads(row["result_json"]) if row["result_json"] else None
        return DebateSession(
            id=row["id"],
            session_id=row["session_id"],
            topic=row["topic"],
            mode=DebateMode(row["mode"]),
            status=DebateStatus(row["status"]),
            participants=tuple(json.loads(row["participants_json"])),
            moderator=row["moderator"],
            limits=DebateLimits(
                max_rounds=int(row["max_rounds"]),
                timeout_seconds=float(row["timeout_seconds"]),
                quorum=int(row["quorum"]),
            ),
            current_round=int(row["current_round"]),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            result=synthesis_from_dict(result_payload),
            error=row["error"] or "",
            applied_at=row["applied_at"],
        )


def _work_item_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or ""),
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "task_type": str(payload.get("task_type") or "general"),
        "assigned_to": payload.get("assigned_to") or None,
        "dependencies": tuple(payload.get("dependencies") or ()),
        "acceptance_criteria": tuple(payload.get("acceptance_criteria") or ()),
        "priority": str(payload.get("priority") or "medium"),
        "evidence_ids": tuple(payload.get("evidence_ids") or ()),
    }


def _evidence_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or ""),
        "source": str(payload.get("source") or ""),
        "line_start": int(payload.get("line_start") or 0),
        "line_end": int(payload.get("line_end") or 0),
        "excerpt": str(payload.get("excerpt") or ""),
        "claim": str(payload.get("claim") or ""),
    }
