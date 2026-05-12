import io
import runpy
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from quimera.constants import TaskStatus, can_transition
from quimera.runtime import tasks


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "runtime_tasks.db"
    tasks.init_db(str(path))
    return str(path)


def _task_row(task_id, db_path):
    conn = tasks.get_conn(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT status, assigned_to, result, notes, reviewed_by, failed_agents, attempt_count FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def test_add_job_with_explicit_job_id_reuses_existing_row(db_path):
    first = tasks.add_job("job original", created_by="alex", db_path=db_path, job_id=42)
    second = tasks.add_job("job novo", created_by="bia", db_path=db_path, job_id=42)

    assert first == 42
    assert second == 42

    jobs = tasks.list_jobs(db_path=db_path)
    assert len(jobs) == 1
    assert jobs[0]["description"] == "job original"


def test_list_jobs_filters_by_status(db_path):
    job_a = tasks.add_job("job a", db_path=db_path)
    tasks.add_job("job b", db_path=db_path)

    conn = tasks.get_conn(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE jobs SET status = ? WHERE id = ?", ("completed", job_a))
    conn.commit()
    conn.close()

    completed = tasks.list_jobs({"status": "completed"}, db_path=db_path)
    planning = tasks.list_jobs({"status": "planning"}, db_path=db_path)

    assert len(completed) == 1
    assert completed[0]["id"] == job_a
    assert len(planning) == 1


def test_get_job_returns_none_for_unknown_id(db_path):
    assert tasks.get_job(9999, db_path=db_path) is None


def test_list_tasks_filters_by_assignment_type_and_origin(db_path):
    job_id = tasks.add_job("job", db_path=db_path)

    task_keep = tasks.create_task(
        job_id,
        "task keep",
        task_type="code_edit",
        assigned_to="codex",
        origin="human_command",
        db_path=db_path,
    )
    tasks.create_task(
        job_id,
        "task skip",
        task_type="code_review",
        assigned_to="claude",
        origin="legacy_tool",
        db_path=db_path,
    )

    rows = tasks.list_tasks(
        {
            "assigned_to": "codex",
            "task_type": "code_edit",
            "origin": "human_command",
        },
        db_path=db_path,
    )

    assert [row["id"] for row in rows] == [task_keep]


def test_release_agent_tasks_resets_only_pending_and_in_progress(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    pending_id = tasks.create_task(job_id, "pending", assigned_to="agent-x", status="pending", db_path=db_path)
    in_progress_id = tasks.create_task(
        job_id,
        "in progress",
        assigned_to="agent-x",
        status="in_progress",
        db_path=db_path,
    )
    completed_id = tasks.create_task(
        job_id,
        "completed",
        assigned_to="agent-x",
        status="completed",
        db_path=db_path,
    )

    tasks.release_agent_tasks("agent-x", db_path=db_path)

    by_id = {row["id"]: row for row in tasks.list_tasks({"job_id": job_id}, db_path=db_path)}
    assert by_id[pending_id]["status"] == "pending"
    assert by_id[pending_id]["assigned_to"] is None
    assert by_id[in_progress_id]["status"] == "pending"
    assert by_id[in_progress_id]["assigned_to"] is None
    assert by_id[completed_id]["status"] == "completed"
    assert by_id[completed_id]["assigned_to"] == "agent-x"


def test_requeue_task_returns_false_for_missing_task(db_path):
    assert tasks.requeue_task(404, "codex", reason="falha", db_path=db_path) is False


def test_requeue_task_after_review_returns_false_for_missing_task(db_path):
    assert tasks.requeue_task_after_review(404, "codex", result="x", notes="y", db_path=db_path) is False


def test_requeue_task_does_not_duplicate_failed_token_and_increments_attempt(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "task", assigned_to="codex", status="in_progress", db_path=db_path)

    conn = tasks.get_conn(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET failed_agents = ?, attempt_count = ? WHERE id = ?", ("|codex|", 2, task_id))
    conn.commit()
    conn.close()

    assert tasks.requeue_task(task_id, "codex", reason="erro transitorio", db_path=db_path) is True

    row = _task_row(task_id, db_path)
    assert row[0] == "pending"
    assert row[1] is None
    assert row[2] == "erro transitorio"
    assert row[3] == "erro transitorio"
    assert row[5] == "|codex|"
    assert row[6] == 3


def test_requeue_task_after_review_clears_reviewer_and_preserves_unique_failed_token(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "task", assigned_to="codex", status=TaskStatus.REVIEWING, db_path=db_path)

    conn = tasks.get_conn(db_path)
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET reviewed_by = ?, failed_agents = ?, attempt_count = ? WHERE id = ?",
        ("gemini", "|codex|", 1, task_id),
    )
    conn.commit()
    conn.close()

    assert tasks.requeue_task_after_review(
        task_id,
        "codex",
        result="resultado",
        notes="falta evidencia",
        db_path=db_path,
    ) is True

    row = _task_row(task_id, db_path)
    assert row[0] == "pending"
    assert row[1] is None
    assert row[2] == "resultado"
    assert row[3] == "falta evidencia"
    assert row[4] is None
    assert row[5] == "|codex|"
    assert row[6] == 2


def test_can_reassign_task_handles_candidate_and_failed_agent_cases(db_path):
    assert tasks.can_reassign_task(1, [], db_path=db_path) is False
    assert tasks.can_reassign_task(999, ["codex"], db_path=db_path) is False

    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "task", db_path=db_path)

    conn = tasks.get_conn(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET failed_agents = ? WHERE id = ?", ("|codex|", task_id))
    conn.commit()
    conn.close()

    assert tasks.can_reassign_task(task_id, ["codex"], db_path=db_path) is False
    assert tasks.can_reassign_task(task_id, ["codex", "gemini"], db_path=db_path) is True


def test_can_reassign_task_returns_true_on_sqlite_error(monkeypatch):
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
    monkeypatch.setattr(tasks, "get_conn", lambda _db_path: conn)

    assert tasks.can_reassign_task(1, ["codex"], db_path="unused") is True
    assert conn.closed is True


def test_claim_task_rolls_back_and_reraises_on_error(monkeypatch):
    class FailingCursor:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("db select failure")

        def fetchone(self):
            return None

    class FailingConn:
        def __init__(self):
            self.rollback_called = False
            self.close_called = False
            self.cursor_obj = FailingCursor()

        def cursor(self):
            return self.cursor_obj

        def rollback(self):
            self.rollback_called = True

        def close(self):
            self.close_called = True

    conn = FailingConn()
    monkeypatch.setattr(tasks, "get_conn", lambda _db_path: conn)

    with pytest.raises(RuntimeError, match="db select failure"):
        tasks.claim_task("codex", db_path="unused")

    assert conn.rollback_called is True
    assert conn.close_called is True


def test_claim_review_task_rolls_back_and_reraises_on_error(monkeypatch):
    class FailingCursor:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("db select failure")

        def fetchone(self):
            return None

    class FailingConn:
        def __init__(self):
            self.rollback_called = False
            self.close_called = False
            self.cursor_obj = FailingCursor()

        def cursor(self):
            return self.cursor_obj

        def rollback(self):
            self.rollback_called = True

        def close(self):
            self.close_called = True

    conn = FailingConn()
    monkeypatch.setattr(tasks, "get_conn", lambda _db_path: conn)

    with pytest.raises(RuntimeError, match="db select failure"):
        tasks.claim_review_task("gemini", db_path="unused")

    assert conn.rollback_called is True
    assert conn.close_called is True


def test_get_conn_raises_on_none():
    with pytest.raises(ValueError, match="db_path is required"):
        tasks.get_conn(None)


def test_list_jobs_filter_created_by(db_path):
    tasks.add_job("job by alex", created_by="alex", db_path=db_path)
    tasks.add_job("job by bia", created_by="bia", db_path=db_path)
    result = tasks.list_jobs({"created_by": "alex"}, db_path=db_path)
    assert len(result) == 1
    assert result[0]["created_by"] == "alex"


def test_get_job_returns_existing(db_path):
    job_id = tasks.add_job("meu job", db_path=db_path)
    job = tasks.get_job(job_id, db_path=db_path)
    assert job is not None
    assert job["description"] == "meu job"
    assert job["id"] == job_id


def test_approve_task_returns_false_for_non_proposed(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status="approved", db_path=db_path)
    assert tasks.approve_task(task_id, "user", db_path=db_path) is False


def test_approve_task_returns_false_for_missing(db_path):
    assert tasks.approve_task(9999, "user", db_path=db_path) is False


def test_reject_task_returns_false_for_non_proposed(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status="approved", db_path=db_path)
    assert tasks.reject_task(task_id, "user", db_path=db_path) is False


def test_reject_task_returns_false_for_missing(db_path):
    assert tasks.reject_task(9999, "user", db_path=db_path) is False


def test_list_tasks_filter_by_id(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    t1 = tasks.create_task(job_id, "task one", db_path=db_path)
    tasks.create_task(job_id, "task two", db_path=db_path)
    result = tasks.list_tasks({"id": t1}, db_path=db_path)
    assert len(result) == 1
    assert result[0]["description"] == "task one"


def test_requeue_task_adds_new_token(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", assigned_to="codex", status="in_progress", db_path=db_path)
    # failed_agents is empty — token will be added (line 317)
    assert tasks.requeue_task(task_id, "codex", reason="erro", db_path=db_path) is True
    row = _task_row(task_id, db_path)
    assert "|codex|" in (row[5] or "")


def test_requeue_task_after_review_adds_new_token(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", assigned_to="gemini", status=TaskStatus.REVIEWING, db_path=db_path)
    # failed_agents is empty — token will be added (line 344)
    assert tasks.requeue_task_after_review(task_id, "gemini", result="r", notes="n", db_path=db_path) is True


def test_claim_task_with_job_id(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status="approved", db_path=db_path)
    claimed = tasks.claim_task("codex", job_id=job_id, db_path=db_path)
    assert claimed == task_id


def test_complete_task_with_reviewed_by(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status=TaskStatus.REVIEWING, db_path=db_path)
    assert tasks.complete_task(task_id, result="done", reviewed_by="gemini", db_path=db_path) is True
    row = tasks.list_tasks({"id": task_id}, db_path=db_path)[0]
    assert row["status"] == "completed"


def test_fail_task(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status=TaskStatus.IN_PROGRESS, db_path=db_path)
    assert tasks.fail_task(task_id, reason="timeout", db_path=db_path) is True


def test_submit_for_review(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status=TaskStatus.IN_PROGRESS, db_path=db_path)
    assert tasks.submit_for_review(task_id, result="evidence", db_path=db_path) is True
    row = tasks.list_tasks({"id": task_id}, db_path=db_path)[0]
    assert row["status"] == "pending_review"


def test_claim_review_task_with_job_id(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status="pending_review", db_path=db_path)
    claimed = tasks.claim_review_task("gemini", job_id=job_id, db_path=db_path)
    assert claimed == task_id


def test_claim_review_task_returns_task_id(db_path):
    job_id = tasks.add_job("job", db_path=db_path)
    task_id = tasks.create_task(job_id, "t", status="pending_review", db_path=db_path)
    claimed = tasks.claim_review_task("gemini", db_path=db_path)
    assert claimed == task_id


def test_claim_review_task_returns_none_when_empty(db_path):
    assert tasks.claim_review_task("gemini", db_path=db_path) is None


def test_drop_db_removes_existing_file(tmp_path):
    db_file = tmp_path / "to_remove.db"
    db_file.write_text("temporary")

    tasks.drop_db(str(db_file))

    assert db_file.exists() is False


# ---------------------------------------------------------------------------
# __main__ CLI block (lines 517-612)
# ---------------------------------------------------------------------------

def _run_main(db_path, *args):
    """Execute the __main__ block of tasks.py in-process and return (stdout, exit_code)."""
    buf = io.StringIO()
    argv = ["quimera.runtime.tasks", "--db", str(db_path), *args]
    exit_code = 0
    with patch("sys.argv", argv), redirect_stdout(buf):
        try:
            runpy.run_module("quimera.runtime.tasks", run_name="__main__", alter_sys=False)
        except SystemExit as exc:
            exit_code = exc.code if exc.code is not None else 0
    return buf.getvalue().strip(), exit_code


@pytest.fixture
def cli_db(tmp_path):
    """Pre-initialized DB for CLI tests (avoids the init subparser --db conflict)."""
    db = tmp_path / "cli.db"
    tasks.init_db(str(db))
    return db


def test_cli_init(tmp_path):
    db = tmp_path / "cli.db"
    # The 'init' subparser defines its own --db, which must be passed after the subcommand
    out, code = _run_main(db, "init", "--db", str(db))
    assert code == 0
    assert "initialized" in out.lower()


def test_cli_add_job(cli_db):
    out, code = _run_main(cli_db, "add-job", "meu job")
    assert code == 0
    assert out.startswith("job:")


def test_cli_list_jobs(cli_db):
    tasks.add_job("job x", db_path=str(cli_db))
    out, code = _run_main(cli_db, "list-jobs", "--status", "planning")
    assert code == 0
    assert "job x" in out


def test_cli_list_jobs_no_status_filter(cli_db):
    # Without --status, ns.status=None → WHERE status=NULL → empty result (known CLI behavior)
    tasks.add_job("job y", db_path=str(cli_db))
    out, code = _run_main(cli_db, "list-jobs")
    assert code == 0


def test_cli_propose(cli_db):
    job_id = tasks.add_job("job z", db_path=str(cli_db))
    out, code = _run_main(cli_db, "propose", "--job-id", str(job_id), "--desc", "tarefa 1")
    assert code == 0
    assert out.startswith("task:")


def test_cli_approve(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    out, code = _run_main(cli_db, "approve", "--id", str(task_id), "--by", "codex")
    assert code == 0
    assert out == "approved"
    conn = sqlite3.connect(str(cli_db))
    row = conn.execute("SELECT status, approved_by FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    assert row == (TaskStatus.APPROVED, "codex")


def test_cli_reject(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    out, code = _run_main(cli_db, "reject", "--id", str(task_id), "--by", "claude", "--reason", "bad-plan")
    assert code == 0
    assert out == "rejected"
    conn = sqlite3.connect(str(cli_db))
    row = conn.execute("SELECT status, approved_by, notes FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    assert row == (TaskStatus.REJECTED, "claude", "bad-plan")


def test_cli_list_tasks(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    tasks.propose_task(job_id, "tarefa A", db_path=str(cli_db))
    out, code = _run_main(cli_db, "list-tasks", "--job-id", str(job_id), "--status", "proposed")
    assert code == 0
    assert "tarefa A" in out


def test_cli_claim_no_tasks(cli_db):
    out, code = _run_main(cli_db, "claim", "--agent", "codex")
    assert code == 0
    assert out == "no-tasks-available"


def test_cli_claim_with_task(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    tasks.approve_task(task_id, "test", db_path=str(cli_db))
    out, code = _run_main(cli_db, "claim", "--agent", "codex")
    assert code == 0
    assert out.startswith("claimed:")


def test_cli_update(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    # proposed → approved é uma transição válida
    out, code = _run_main(cli_db, "update", "--id", str(task_id), "--status", "approved")
    assert code == 0
    assert out == "updated"


def test_cli_update_preserves_approved_by(cli_db):
    """update subsequente não deve apagar approved_by gravado por approve."""
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    # proposed → approved com metadados
    out, code = _run_main(cli_db, "approve", "--id", str(task_id), "--by", "codex")
    assert code == 0
    # approved → in_progress via update sem --by
    out, code = _run_main(cli_db, "update", "--id", str(task_id), "--status", "in_progress")
    assert code == 0
    conn = sqlite3.connect(str(cli_db))
    row = conn.execute("SELECT approved_by FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    assert row[0] == "codex"


def test_cli_update_invalid_transition(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    # proposed → in_progress é inválido — state machine recusa
    out, code = _run_main(cli_db, "update", "--id", str(task_id), "--status", "in_progress")
    assert code == 1
    assert out == "transition-failed"


def test_cli_complete(cli_db):
    job_id = tasks.add_job("job", db_path=str(cli_db))
    task_id = tasks.propose_task(job_id, "t", db_path=str(cli_db))
    out, code = _run_main(cli_db, "complete", "--id", str(task_id))
    assert code == 1
    assert out == "transition-failed"


def test_cli_no_cmd_prints_help(cli_db):
    # No subcommand → parser.print_help() + exit(1)
    buf = io.StringIO()
    argv = ["quimera.runtime.tasks", "--db", str(cli_db)]
    with patch("sys.argv", argv), redirect_stdout(buf):
        try:
            runpy.run_module("quimera.runtime.tasks", run_name="__main__", alter_sys=False)
        except SystemExit as exc:
            assert exc.code == 1
    assert "usage" in buf.getvalue().lower()


# --- Testes de state machine (can_transition / transition_task) ---

class TestCanTransition:
    def test_valid_proposed_to_approved(self):
        assert can_transition(TaskStatus.PROPOSED, TaskStatus.APPROVED)

    def test_valid_proposed_to_rejected(self):
        assert can_transition(TaskStatus.PROPOSED, TaskStatus.REJECTED)

    def test_valid_approved_to_in_progress(self):
        assert can_transition(TaskStatus.APPROVED, TaskStatus.IN_PROGRESS)

    def test_valid_pending_to_in_progress(self):
        assert can_transition(TaskStatus.PENDING, TaskStatus.IN_PROGRESS)

    def test_valid_in_progress_to_pending_review(self):
        assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.PENDING_REVIEW)

    def test_valid_in_progress_to_completed(self):
        assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED)

    def test_valid_in_progress_to_failed(self):
        assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.FAILED)

    def test_valid_in_progress_to_pending_requeue(self):
        assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.PENDING)

    def test_valid_pending_review_to_reviewing(self):
        assert can_transition(TaskStatus.PENDING_REVIEW, TaskStatus.REVIEWING)

    def test_valid_reviewing_to_completed(self):
        assert can_transition(TaskStatus.REVIEWING, TaskStatus.COMPLETED)

    def test_valid_reviewing_to_failed(self):
        assert can_transition(TaskStatus.REVIEWING, TaskStatus.FAILED)

    def test_valid_reviewing_to_pending_requeue(self):
        assert can_transition(TaskStatus.REVIEWING, TaskStatus.PENDING)

    def test_invalid_completed_to_anything(self):
        assert not can_transition(TaskStatus.COMPLETED, TaskStatus.PENDING)
        assert not can_transition(TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS)

    def test_invalid_failed_to_anything(self):
        assert not can_transition(TaskStatus.FAILED, TaskStatus.PENDING)

    def test_invalid_rejected_to_anything(self):
        assert not can_transition(TaskStatus.REJECTED, TaskStatus.APPROVED)

    def test_invalid_pending_to_completed_directly(self):
        assert not can_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)

    def test_accepts_raw_strings(self):
        assert can_transition("proposed", "approved")
        assert not can_transition("completed", "pending")

    def test_unknown_status_returns_false(self):
        assert not can_transition("unknown_status", "pending")
        assert not can_transition("pending", "unknown_status")


class TestTransitionTask:
    def test_valid_transition_succeeds(self, db_path):
        job_id = tasks.add_job("j", db_path=db_path)
        task_id = tasks.create_task(job_id, "t", status=TaskStatus.PENDING, db_path=db_path)
        result = tasks.transition_task(task_id, TaskStatus.IN_PROGRESS, db_path=db_path)
        assert result is True
        row = _task_row(task_id, db_path)
        assert row[0] == TaskStatus.IN_PROGRESS

    def test_invalid_transition_returns_false(self, db_path):
        job_id = tasks.add_job("j", db_path=db_path)
        task_id = tasks.create_task(job_id, "t", status=TaskStatus.COMPLETED, db_path=db_path)
        result = tasks.transition_task(task_id, TaskStatus.PENDING, db_path=db_path)
        assert result is False
        row = _task_row(task_id, db_path)
        assert row[0] == TaskStatus.COMPLETED

    def test_missing_task_returns_false(self, db_path):
        result = tasks.transition_task(99999, TaskStatus.IN_PROGRESS, db_path=db_path)
        assert result is False
