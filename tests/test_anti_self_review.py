import pytest
from unittest.mock import MagicMock, patch
from quimera.runtime.tasks import (
    init_db, create_task, claim_task, submit_for_review, 
    claim_review_task, complete_task, get_conn, add_job
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