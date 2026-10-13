"""Schema SQLite dos workflows e predicados SQL de elegibilidade de claim.

Sem dependências do pacote ``tasks``: ``TaskRepository`` importa este módulo
para criar as tabelas no mesmo banco e para aplicar o gate de claim, sem criar
ciclo de importação com o repositório de workflows.
"""
from __future__ import annotations

import sqlite3

WORKFLOW_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS workflows (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          INTEGER NOT NULL,
        objective       TEXT NOT NULL,
        coordinator     TEXT NOT NULL,
        status          TEXT NOT NULL,
        status_reason   TEXT,
        plan_version    INTEGER NOT NULL DEFAULT 1,
        global_criteria TEXT NOT NULL DEFAULT '[]',
        constraints     TEXT NOT NULL DEFAULT '[]',
        replans         INTEGER NOT NULL DEFAULT 0,
        consults        INTEGER NOT NULL DEFAULT 0,
        attention       TEXT,
        result          TEXT,
        publication_key TEXT NOT NULL UNIQUE,
        created_at      DATETIME NOT NULL,
        updated_at      DATETIME NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_plans (
        workflow_id INTEGER NOT NULL,
        version     INTEGER NOT NULL,
        plan_json   TEXT NOT NULL,
        plan_hash   TEXT NOT NULL,
        created_at  DATETIME NOT NULL,
        PRIMARY KEY (workflow_id, version),
        FOREIGN KEY (workflow_id) REFERENCES workflows (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_tasks (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id       INTEGER NOT NULL,
        item_id           TEXT NOT NULL,
        task_id           INTEGER NOT NULL UNIQUE,
        plan_version      INTEGER NOT NULL,
        superseded_in     INTEGER,
        writes            INTEGER NOT NULL DEFAULT 0,
        evidence_expected TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY (workflow_id) REFERENCES workflows (id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_tasks_current
    ON workflow_tasks (workflow_id, item_id) WHERE superseded_in IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS task_attempts (
        task_id        INTEGER NOT NULL,
        attempt        INTEGER NOT NULL,
        agent          TEXT NOT NULL,
        run_id         TEXT,
        status         TEXT NOT NULL,
        failure_reason TEXT,
        handoff        TEXT,
        started_at     DATETIME NOT NULL,
        finished_at    DATETIME,
        PRIMARY KEY (task_id, attempt, agent),
        FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
    )
    """,
)

# Uma task de workflow só é reivindicável para execução com o workflow em
# ``running`` e enquanto não substituída por um replanejamento. Tasks de escrita
# são exclusivas: esperam outra escrita em execução ou em revisão (até o aceite),
# exceto revisões de workflows pausados/encerrados, que não progrediriam e
# travariam os demais workflows.
EXEC_CLAIM_GATE_SQL = """
    AND NOT EXISTS (
        SELECT 1 FROM workflow_tasks AS gate_link
        JOIN workflows AS gate_wf ON gate_wf.id = gate_link.workflow_id
        WHERE gate_link.task_id = candidate.id
          AND (gate_wf.status != 'running' OR gate_link.superseded_in IS NOT NULL)
    )
    AND NOT (
        COALESCE((SELECT gate_own.writes FROM workflow_tasks AS gate_own
                  WHERE gate_own.task_id = candidate.id), 0) = 1
        AND EXISTS (
            SELECT 1 FROM workflow_tasks AS gate_writer
            JOIN tasks AS gate_task ON gate_task.id = gate_writer.task_id
            JOIN workflows AS gate_writer_wf ON gate_writer_wf.id = gate_writer.workflow_id
            WHERE gate_writer.writes = 1
              AND gate_writer.superseded_in IS NULL
              AND gate_task.id != candidate.id
              AND (
                  gate_task.status = 'in_progress'
                  OR (gate_task.status IN ('pending_review', 'reviewing')
                      AND gate_writer_wf.status IN ('running', 'blocked'))
              )
        )
    )
"""

# Revisões seguem enquanto o workflow está ativo (running) ou aguardando
# decisão (blocked); pausado ou encerrado, nada novo começa.
REVIEW_CLAIM_GATE_SQL = """
    AND NOT EXISTS (
        SELECT 1 FROM workflow_tasks AS gate_link
        JOIN workflows AS gate_wf ON gate_wf.id = gate_link.workflow_id
        WHERE gate_link.task_id = tasks.id
          AND (gate_wf.status NOT IN ('running', 'blocked')
               OR gate_link.superseded_in IS NOT NULL)
    )
"""


def ensure_workflow_schema(conn: sqlite3.Connection) -> None:
    """Cria as tabelas de workflow (idempotente) na conexão informada."""
    for statement in WORKFLOW_SCHEMA:
        conn.execute(statement)
