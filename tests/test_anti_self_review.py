import pytest

from quimera.runtime.tasks import (
    init_db, create_task, claim_task, submit_for_review,
    claim_review_task, complete_task, get_conn, add_job, requeue_task_after_review
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_anti_self_review.db"
    init_db(str(path))
    return path


def test_executor_cannot_review_own_task(db_path):
    """O agente que executa uma tarefa NÃO pode revisá-la."""
    job_id = add_job("job1", db_path=str(db_path))
    create_task(job_id, "test task", priority="medium", task_type="general", db_path=str(db_path))

    task_id = claim_task("claude", job_id=job_id, db_path=str(db_path))
    assert task_id is not None

    submit_for_review(task_id, result="done", db_path=str(db_path))

    review_id = claim_review_task("claude", job_id=job_id, db_path=str(db_path))
    assert review_id is None, "Executor NÃO deveria poder revisar própria task"


def test_other_agent_can_review(db_path):
    """Outro agente PODE revisar a tarefa."""
    job_id = add_job("job1", db_path=str(db_path))
    create_task(job_id, "test task", priority="medium", task_type="general", db_path=str(db_path))

    task_id = claim_task("claude", job_id=job_id, db_path=str(db_path))
    assert task_id is not None

    submit_for_review(task_id, result="done", db_path=str(db_path))

    review_id = claim_review_task("gemini", job_id=job_id, db_path=str(db_path))
    assert review_id is not None, "Outro agente deveria poder revisar"

    complete_task(review_id, result="done", reviewed_by="gemini", db_path=str(db_path))

    conn = get_conn(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT status, reviewed_by FROM tasks WHERE id = ?", (review_id,))
    row = cur.fetchone()
    conn.close()

    assert row[0] == "completed"
    assert row[1] == "gemini"


def test_rejected_self_review_returns_to_pending_review(db_path):
    """Quando executor tenta revisar, task volta para pending_review."""
    job_id = add_job("job1", db_path=str(db_path))
    create_task(job_id, "test task", priority="medium", task_type="general", db_path=str(db_path))

    task_id = claim_task("claude", job_id=job_id, db_path=str(db_path))
    submit_for_review(task_id, result="done", db_path=str(db_path))

    review_id = claim_review_task("claude", job_id=job_id, db_path=str(db_path))
    assert review_id is None

    conn = get_conn(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    conn.close()

    assert row[0] == "pending_review", "Task deveria voltar ao estado pending_review"


def test_failed_reviewer_cannot_reclaim_same_review(db_path):
    """Depois de falhar no review, a task deve passar para outro agente."""
    job_id = add_job("job1", db_path=str(db_path))
    create_task(job_id, "test task", priority="medium", task_type="general", db_path=str(db_path))

    task_id = claim_task("claude", job_id=job_id, db_path=str(db_path))
    submit_for_review(task_id, result="done", db_path=str(db_path))

    review_id = claim_review_task("gemini", job_id=job_id, db_path=str(db_path))
    assert review_id == task_id

    from quimera.runtime.tasks import update_task

    update_task(task_id, "pending_review", result="done", notes="falha transitória", db_path=str(db_path))

    assert claim_review_task("gemini", job_id=job_id, db_path=str(db_path)) is None
    assert claim_review_task("codex", job_id=job_id, db_path=str(db_path)) == task_id


def test_review_rejection_forces_executor_failover(db_path):
    """Quando o review rejeita, o executor anterior não pode reclamar a task."""
    job_id = add_job("job1", db_path=str(db_path))
    create_task(job_id, "test task", priority="medium", task_type="general", db_path=str(db_path))

    task_id = claim_task("claude", job_id=job_id, db_path=str(db_path))
    submit_for_review(task_id, result="done", db_path=str(db_path))

    review_id = claim_review_task("gemini", job_id=job_id, db_path=str(db_path))
    assert review_id == task_id

    assert requeue_task_after_review(
        task_id,
        "claude",
        result="done",
        notes="RETENTATIVA\nfaltou evidência",
        db_path=str(db_path),
    )

    conn = get_conn(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "SELECT status, assigned_to, result, notes, reviewed_by, failed_agents, attempt_count FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()

    assert row == (
        "pending",
        None,
        "done",
        "RETENTATIVA\nfaltou evidência",
        None,
        "|claude|",
        1,
    )
    assert claim_task("claude", job_id=job_id, db_path=str(db_path)) is None
    assert claim_task("codex", job_id=job_id, db_path=str(db_path)) == task_id


def test_submit_for_review_resets_previous_reviewer(db_path):
    """Novo ciclo de review deve liberar reviewers anteriores."""
    job_id = add_job("job1", db_path=str(db_path))
    create_task(job_id, "test task", priority="medium", task_type="general", db_path=str(db_path))

    task_id = claim_task("claude", job_id=job_id, db_path=str(db_path))
    submit_for_review(task_id, result="done", db_path=str(db_path))
    review_id = claim_review_task("gemini", job_id=job_id, db_path=str(db_path))
    assert review_id == task_id

    requeue_task_after_review(
        task_id,
        "claude",
        result="done",
        notes="RETENTATIVA\nfaltou evidência",
        db_path=str(db_path),
    )

    assert claim_task("codex", job_id=job_id, db_path=str(db_path)) == task_id
    submit_for_review(task_id, result="done novamente", db_path=str(db_path))
    assert claim_review_task("gemini", job_id=job_id, db_path=str(db_path)) == task_id
