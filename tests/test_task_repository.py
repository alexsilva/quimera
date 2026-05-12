import sqlite3

import pytest

from quimera.app.task_repository import TaskRepository
from quimera.constants import TaskStatus
from quimera.runtime import tasks as runtime_tasks
from quimera.runtime.models import JobRecord, TaskRecord


@pytest.fixture
def repository(tmp_path):
    db_path = tmp_path / "task_repository.db"
    return TaskRepository(str(db_path))


def _task_row(task_id, db_path):
    conn = runtime_tasks.get_conn(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, assigned_to, result, notes, reviewed_by, failed_agents, attempt_count FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def test_init_requires_db_path():
    with pytest.raises(ValueError, match="db_path is required"):
        TaskRepository("")


def test_create_task_and_list_tasks(repository):
    job_id = runtime_tasks.add_job("job repo", db_path=repository.db_path)
    task_id = repository.create_task(
        job_id,
        "implementar wrapper",
        task_type="code_edit",
        assigned_to="codex",
        origin="human_command",
    )

    rows = repository.list_tasks({"job_id": job_id, "assigned_to": "codex"})
    assert [row.id for row in rows] == [task_id]
    assert rows[0].description == "implementar wrapper"
    assert rows[0].task_type == "code_edit"
    assert isinstance(rows[0], TaskRecord)


def test_get_job_returns_existing_and_none(repository):
    job_id = runtime_tasks.add_job("job detail", db_path=repository.db_path)
    job = repository.get_job(job_id)

    assert job is not None
    assert isinstance(job, JobRecord)
    assert job.id == job_id
    assert repository.get_job(999_999) is None


def test_fail_task(repository):
    job_id = runtime_tasks.add_job("job fail", db_path=repository.db_path)
    task_id = repository.create_task(job_id, "falhar", status=TaskStatus.IN_PROGRESS)

    assert repository.fail_task(task_id, reason="timeout") is True

    row = _task_row(task_id, repository.db_path)
    assert row[0] == TaskStatus.FAILED
    assert row[2] == "timeout"
    assert row[3] == "timeout"


def test_requeue_task(repository):
    job_id = runtime_tasks.add_job("job requeue", db_path=repository.db_path)
    task_id = repository.create_task(job_id, "retentar", assigned_to="codex", status=TaskStatus.IN_PROGRESS)

    assert repository.requeue_task(task_id, "codex", reason="falha transitoria") is True

    row = _task_row(task_id, repository.db_path)
    assert row[0] == TaskStatus.PENDING
    assert row[1] is None
    assert row[2] == "falha transitoria"
    assert row[3] == "falha transitoria"
    assert "|codex|" in (row[5] or "")
    assert row[6] == 1


def test_submit_for_review_and_complete_task(repository):
    job_id = runtime_tasks.add_job("job review", db_path=repository.db_path)
    review_task_id = repository.create_task(job_id, "revisar", status=TaskStatus.IN_PROGRESS)
    direct_task_id = repository.create_task(job_id, "fechar", status=TaskStatus.IN_PROGRESS)

    assert repository.submit_for_review(review_task_id, result="resultado da execucao") is True
    pending_review = repository.list_tasks({"id": review_task_id})[0]
    assert pending_review.status == TaskStatus.PENDING_REVIEW
    assert pending_review.result == "resultado da execucao"

    conn = runtime_tasks.get_conn(repository.db_path)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = ? WHERE id = ?", (TaskStatus.REVIEWING, review_task_id))
    conn.commit()
    conn.close()

    assert repository.complete_task(review_task_id, result="ok", reviewed_by="gemini") is True
    assert repository.complete_task(direct_task_id, result="ok sem review") is True

    by_id = {row.id: row for row in repository.list_tasks({"job_id": job_id})}
    assert by_id[review_task_id].status == TaskStatus.COMPLETED
    assert by_id[direct_task_id].status == TaskStatus.COMPLETED


def test_requeue_task_after_review_clears_reviewer(repository):
    job_id = runtime_tasks.add_job("job review requeue", db_path=repository.db_path)
    task_id = repository.create_task(job_id, "ajustar", assigned_to="codex", status=TaskStatus.REVIEWING)

    conn = runtime_tasks.get_conn(repository.db_path)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET reviewed_by = ? WHERE id = ?", ("gemini", task_id))
    conn.commit()
    conn.close()

    assert repository.requeue_task_after_review(task_id, "codex", result="novo resultado", notes="pedir ajuste") is True

    row = _task_row(task_id, repository.db_path)
    assert row[0] == TaskStatus.PENDING
    assert row[1] is None
    assert row[2] == "novo resultado"
    assert row[3] == "pedir ajuste"
    assert row[4] is None
    assert row[6] == 1


def test_transition_task_preserves_omitted_fields(repository):
    job_id = runtime_tasks.add_job("job transition", db_path=repository.db_path)
    task_id = repository.create_task(job_id, "transicionar", status=TaskStatus.IN_PROGRESS)
    runtime_tasks.update_task(
        task_id,
        TaskStatus.IN_PROGRESS,
        result="resultado inicial",
        notes="nota inicial",
        db_path=repository.db_path,
    )

    assert repository.transition_task(task_id, TaskStatus.PENDING_REVIEW) is True
    first = repository.list_tasks({"id": task_id})[0]
    assert first.result == "resultado inicial"
    assert first.notes == "nota inicial"

    assert repository.transition_task(task_id, TaskStatus.REVIEWING, notes="validando") is True
    second = repository.list_tasks({"id": task_id})[0]
    assert second.result == "resultado inicial"
    assert second.notes == "validando"


def test_can_reassign_task(repository):
    assert repository.can_reassign_task(1, []) is False
    assert repository.can_reassign_task(999, ["codex"]) is False

    job_id = runtime_tasks.add_job("job can reassign", db_path=repository.db_path)
    task_id = repository.create_task(job_id, "roteamento", status=TaskStatus.IN_PROGRESS)
    conn = runtime_tasks.get_conn(repository.db_path)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET failed_agents = ? WHERE id = ?", ("|codex|", task_id))
    conn.commit()
    conn.close()

    assert repository.can_reassign_task(task_id, ["codex"]) is False
    assert repository.can_reassign_task(task_id, ["codex", "gemini"]) is True


def test_can_reassign_task_returns_true_on_sqlite_error(repository, monkeypatch):
    class BrokenCursor:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.Error("boom")

        def fetchone(self):
            return None

    class BrokenConn:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return BrokenCursor()

        def close(self):
            self.closed = True

    conn = BrokenConn()
    monkeypatch.setattr(repository, "_conn", lambda: conn)

    assert repository.can_reassign_task(1, ["codex"]) is True
    assert conn.closed is True
